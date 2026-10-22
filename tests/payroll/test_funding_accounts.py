"""Funding accounts per legal entity: encrypted details, purpose, signatories, offered file formats, SANDBOX-only
provider profile, entity scope, row_version, step-up, reveal, the read API for payments, and three-tenant
isolation (3.7 §6, §12; 3.9 §3.8 and §4 Funding accounts).

EC-BNK-04, EC-BNK-05, EC-BNK-11, EC-SEC-01.
"""

import json

import pytest

from tests.helpers import create_employee
from tests.payroll.test_bank_accounts import (  # noqa: F401  (bank_world and world are fixtures)
    AE_IBAN,
    IN_NUMBER,
    MASK,
    bank_world,
    in_account,
    ae_account,
    place,
    role,
    session,
    sql,
    step_up,
    tenant,
    user_with_role,
    world,
)

LIST = "/api/v1/payroll/funding-accounts"


def entity_url(entity_id):
    return f"/api/v1/payroll/legal-entities/{entity_id}/funding-accounts"


def funding_body(w, details=None, **extra):
    return {"name": "Payroll HDFC", "purpose": "PAYROLL", "signatory_user_ids": [w["u"]["fay"].user_id],
            "formats": ["GENERIC_CSV"],
            "details": details or in_account("Acme Corp India Pvt Ltd", account_type="CURRENT"), **extra}


def create(client, entity_id, body, expect=201):
    step_up(client)
    r = client.post(entity_url(entity_id), json=body)
    assert r.status_code == expect, r.text
    return r.json()


@pytest.fixture
def funding_world(bank_world, app):
    """bank_world plus Tia, who manages funding accounts of AE01 only."""
    w = bank_world
    acme = w["u"]["founder"]
    tia = create_employee(acme, "Tia", hire_date="2024-01-15")
    place(tia["id"], w["le01"]["id"], w["groups"]["IN"])
    treasurer = role(acme, "Gulf treasurer", "LEGAL_ENTITY", ("payroll:funding_accounts:manage",))
    w["u"]["tia"] = user_with_role(app, acme, tia, treasurer, [w["ae"]["id"]])
    return w


def test_finance_creates_masked_encrypted_funding_accounts_with_step_up(funding_world):
    w = funding_world
    fay, le01 = w["u"]["fay"], w["le01"]["id"]
    r = fay.post(entity_url(le01), json=funding_body(w))
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    account = create(fay, le01, funding_body(w))
    assert account["account_masked"] == f"{MASK}6789" and account["status"] == "ACTIVE"
    assert account["signatory_user_ids"] == [fay.user_id] and account["formats"] == ["GENERIC_CSV"]
    assert IN_NUMBER not in json.dumps(account)
    [(enc, fp)] = sql("SELECT account_number_enc, fingerprint FROM funding_accounts WHERE id = :i", i=account["id"])
    assert enc and IN_NUMBER not in enc and IN_NUMBER not in fp
    assert [a["id"] for a in fay.get(LIST).json()["items"]] == [account["id"]]
    assert [a["id"] for a in fay.get(entity_url(le01)).json()["items"]] == [account["id"]]
    assert fay.get(entity_url(w["ae"]["id"])).json()["items"] == []
    audit = sql("SELECT details::text FROM audit_logs WHERE company_id = :c AND entity_type = 'funding_account'",
                c=w["cid"])
    assert audit and all(IN_NUMBER not in d for (d,) in audit)
    # Same account again for the same entity: duplicate.
    r = fay.post(entity_url(le01), json=funding_body(w, name="Again"))
    assert (r.status_code, r.json()["code"]) == (409, "FUNDING_ACCOUNT_DUPLICATE")
    uae = create(fay, w["ae"]["id"], funding_body(w, name="Payroll AED", details=ae_account("Acme Gulf FZ-LLC")))
    assert uae["account_masked"] == f"AE{MASK}3456" and uae["currency"] == "AED"


def test_ec_bnk_04_ec_bnk_05_formats_signatories_and_profiles_are_validated(funding_world):
    w = funding_world
    fay, le01, ae = w["u"]["fay"], w["le01"]["id"], w["ae"]["id"]
    step_up(fay)
    cases = [
        (ae, funding_body(w, details=ae_account("Acme Gulf", routing={"ifsc": "HDFC0001234"})), 422,
         "FIELD_NOT_FOR_COUNTRY"),
        (ae, funding_body(w, details=ae_account("Acme Gulf", iban="AE080331234567890123456")), 422, "IBAN_CHECKSUM"),
        (le01, funding_body(w, formats=["AE_WPS_SIF"]), 400, "FORMAT_NOT_OFFERED"),
        (le01, funding_body(w, formats=["SWIFT_MT101"]), 400, "FORMAT_UNKNOWN"),
        (le01, funding_body(w, provider_profile="LIVE_BANK"), 400, "PROVIDER_PROFILE_INVALID"),
        (le01, funding_body(w, signatory_user_ids=[w["u"]["beta"].user_id]), 404, None),
        (le01, funding_body(w, signatory_user_ids=[]), 422, None),
        (le01, funding_body(w, purpose="SAVINGS"), 422, None),
        (le01, {**funding_body(w), "company_id": 1}, 422, None),
        (le01, {**funding_body(w), "status": "INACTIVE"}, 422, None),
    ]
    for entity, body, status, code in cases:
        r = fay.post(entity_url(entity), json=body)
        assert r.status_code == status, (body, r.text)
        if code:
            assert r.json()["code"] == code, r.text
    sandbox = create(fay, le01, funding_body(w, provider_profile="sandbox", purpose="REIMBURSEMENTS"))
    assert sandbox["provider_profile"] == "SANDBOX" and sandbox["sandbox"] is True
    assert sql("SELECT count(*) FROM funding_accounts WHERE company_id = :c", c=w["cid"])[0][0] == 1


def test_updates_need_row_version_and_step_up_for_bank_details(funding_world):
    w = funding_world
    fay, le01 = w["u"]["fay"], w["le01"]["id"]
    account = create(fay, le01, funding_body(w))
    url = f"{LIST}/{account['id']}"
    r = fay.patch(url, json={"name": "Renamed"})
    assert (r.status_code, r.json()["code"]) == (400, "ROW_VERSION_REQUIRED")
    r = fay.patch(url, json={"name": "Renamed", "row_version": account["row_version"] + 5})
    assert (r.status_code, r.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    r = fay.patch(url, json={"row_version": account["row_version"]})
    assert (r.status_code, r.json()["code"]) == (400, "NO_CHANGES")
    r = fay.patch(url, json={"name": "Renamed", "signatory_user_ids": [fay.user_id, w["u"]["ava"].user_id]},
                  headers={"If-Match": str(account["row_version"])})
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["name"] == "Renamed" and updated["row_version"] == account["row_version"] + 1
    assert updated["signatory_user_ids"] == sorted([fay.user_id, w["u"]["ava"].user_id])
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=fay.user_id)
    new_details = in_account("Acme Corp India Pvt Ltd", "50100123456700", account_type="CURRENT")
    r = fay.patch(url, json={"details": new_details, "row_version": updated["row_version"]})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(fay)
    r = fay.patch(url, json={"details": new_details, "row_version": updated["row_version"]})
    assert r.status_code == 200 and r.json()["account_masked"] == f"{MASK}6700", r.text
    [(details,)] = sql("SELECT details::text FROM audit_logs WHERE action = 'FUNDING_ACCOUNT_UPDATED' "
                       "AND entity_id = :e AND details::text LIKE '%bank_details%'", e=str(account["id"]))
    assert "50100123456700" not in details and IN_NUMBER not in details and f"{MASK}6700" in details
    # Deactivation (reason + step-up): never deleted; inactive accounts cannot be edited or release batches.
    r = fay.post(f"{url}/deactivate", json={"reason": "x"})
    assert (r.status_code, r.json()["code"]) == (400, "REASON_REQUIRED")
    r = fay.post(f"{url}/deactivate", json={"reason": "Account closed at the bank"})
    assert r.status_code == 200 and r.json()["status"] == "INACTIVE"
    r = fay.patch(url, json={"name": "Again", "row_version": r.json()["row_version"]})
    assert (r.status_code, r.json()["code"]) == (409, "FUNDING_ACCOUNT_INACTIVE")
    assert fay.get(LIST, params={"status": "INACTIVE"}).json()["items"][0]["id"] == account["id"]
    from app.core.errors import AppError
    from app.payroll.services import funding_accounts

    with session(w["cid"]) as (db, _):
        with pytest.raises(AppError) as info:
            funding_accounts.require_signatory(db, w["cid"], account["id"], fay.user_id)
        assert info.value.code == "FUNDING_ACCOUNT_INACTIVE"


def test_entity_scope_permissions_and_reveal(funding_world):
    w = funding_world
    fay, tia, rex, pia, emma = (w["u"][k] for k in ("fay", "tia", "rex", "pia", "emma"))
    india = create(fay, w["le01"]["id"], funding_body(w))
    gulf = create(tia, w["ae"]["id"], funding_body(w, name="Payroll AED", details=ae_account("Acme Gulf FZ-LLC"),
                                                   signatory_user_ids=[tia.user_id]))
    assert [a["id"] for a in tia.get(LIST).json()["items"]] == [gulf["id"]]
    assert tia.get(f"{LIST}/{india['id']}").status_code == 404
    assert tia.get(entity_url(w["le01"]["id"])).status_code == 404
    step_up(tia)
    assert tia.post(entity_url(w["le01"]["id"]), json=funding_body(w)).status_code == 404
    assert tia.patch(f"{LIST}/{india['id']}", json={"name": "x", "row_version": 1}).status_code == 404
    for client in (pia, emma):
        assert client.get(LIST).status_code == 403
        assert client.get(f"{LIST}/{india['id']}").status_code == 403
    assert fay.get(entity_url(999999)).status_code == 404
    # Reveal: read_full + reason + step-up, audited; finance (masked only) cannot.
    url = f"{LIST}/{gulf['id']}/reveal"
    step_up(fay)
    assert fay.post(url, json={"reason": "Bank asked to confirm the IBAN"}).status_code == 403
    step_up(rex)
    r = rex.post(url, json={"reason": "Bank asked to confirm the IBAN"})
    assert r.status_code == 200 and r.json()["iban"] == AE_IBAN and "no-store" in r.headers["cache-control"]
    [(details,)] = sql("SELECT details::text FROM audit_logs WHERE action = 'BANK_DETAILS_REVEALED' "
                       "AND entity_type = 'funding_account'")
    assert AE_IBAN not in details and "Bank asked" in details
    # Read API for payments.
    from app.core.errors import AppError
    from app.payroll.services import funding_accounts

    with session(w["cid"]) as (db, _):
        snapshot = funding_accounts.funding_snapshot(db, w["cid"], gulf["id"])
        assert snapshot["account_masked"] == f"AE{MASK}3456" and snapshot["encrypted"]["iban"]
        assert funding_accounts.funding_plaintext(db, w["cid"], snapshot)["iban"] == AE_IBAN
        assert funding_accounts.require_signatory(db, w["cid"], gulf["id"], tia.user_id).id == gulf["id"]
        with pytest.raises(AppError) as info:
            funding_accounts.require_signatory(db, w["cid"], gulf["id"], fay.user_id)
        assert info.value.code == "FUNDING_SIGNATORY"


def test_ec_sec_01_funding_accounts_are_isolated_across_three_tenants(funding_world):
    w = funding_world
    account = create(w["u"]["fay"], w["le01"]["id"], funding_body(w))
    for key in ("beta", "cobalt"):
        client = w["u"][key]
        step_up(client)
        assert client.get(f"{LIST}/{account['id']}").status_code in (403, 404)
        r = client.get(LIST)
        assert r.status_code == 403 or r.json()["items"] == []
        assert client.get(entity_url(w["le01"]["id"])).status_code in (403, 404)
        assert client.post(entity_url(w["le01"]["id"]), json=funding_body(w)).status_code in (403, 404)
        assert client.patch(f"{LIST}/{account['id']}", json={"name": "x", "row_version": 1}).status_code in (403, 404)
        assert client.post(f"{LIST}/{account['id']}/reveal", json={"reason": "cross tenant attempt"}).status_code \
            in (403, 404)
        with tenant(client.company_id) as conn:
            from sqlalchemy import text

            assert conn.execute(text("SELECT count(*) FROM funding_accounts")).scalar() == 0
            assert conn.execute(text("UPDATE funding_accounts SET name = 'hijack' WHERE id = :i"),
                                {"i": account["id"]}).rowcount == 0
        from app.core.errors import AppError
        from app.payroll.services import funding_accounts

        with session(client.company_id) as (db, _):
            with pytest.raises(AppError):
                funding_accounts.funding_snapshot(db, client.company_id, account["id"])
    assert sql("SELECT name FROM funding_accounts WHERE id = :i", i=account["id"])[0][0] == "Payroll HDFC"
