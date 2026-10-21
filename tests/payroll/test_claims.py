"""Reimbursement claims: policy claim types and limits, receipts in private storage, duplicate fingerprints,
foreign-currency conversion, approvals, payout routes, self-service and scope (3.4 §14, 3.1 §5.10, 3.9 §3.5/§4),
plus row-level isolation of the claim tables across three tenants.

EC-VAR-04, EC-VAR-09, EC-VAR-15, EC-SEC-01, EC-SEC-05.
"""

import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.helpers import create_employee
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    CHECK,
    DENIED,
    FOREIGN_KEY,
    activate_policy,
    audit_actions,
    expect_error,
    month_of,
    pay_group,
    place,
    session,
    set_setting,
    sql,
    tenant,
    world,
)

STAFF = "/api/v1/payroll/reimbursement-claims"
MY = "/api/v1/me/payroll/reimbursement-claims"
POLICY = {
    "claim_types": [
        {"code": "MEALS", "name": "Meals", "limit_per_claim": {"amount": "1000.00", "currency": "INR"},
         "limit_per_month": {"amount": "1500.00", "currency": "INR"},
         "receipt_required_above": {"amount": "500.00", "currency": "INR"}},
        {"code": "TRAVEL", "name": "Travel", "taxability": "EXEMPT"},
    ],
    "submission_window_days": 60,
    "duplicate_receipt_check": True,
}


def pdf(tag):
    return b"%PDF-1.4\n% receipt " + tag.encode() + b"\n%%EOF\n"


def claim_body(w, claim_type="TRAVEL", amount="300.00", currency="INR", days_ago=2, **extra):
    return {"claim_type": claim_type, "expense_date": (w["today"] - timedelta(days=days_ago)).isoformat(),
            "amount": {"amount": amount, "currency": currency}, **extra}


def submit(client, data, url=MY):
    r = client.post(url, json=data)
    assert r.status_code == 201, r.text
    return r.json()


def receipt(client, claim_id, content, url=MY, name="bill.pdf"):
    return client.post(f"{url}/{claim_id}/receipts", files={"file": (name, content, "application/pdf")})


def staff_get(client, claim_id):
    r = client.get(f"{STAFF}/{claim_id}")
    assert r.status_code == 200, r.text
    return r.json()


def fx_rate(company_id, base, quote, rate, as_of):
    sql("INSERT INTO fx_rates (company_id, base_currency, quote_currency, rate, rate_type, source, as_of, fetched_at, "
        "status, created_at, updated_at) VALUES (:c, :b, :q, :r, 'MID', 'MANUAL', :d, now(), 'ACTIVE', now(), now())",
        c=company_id, b=base, q=quote, r=Decimal(rate), d=as_of)


@pytest.fixture
def claims_world(world):
    set_setting(world["cid"], "payroll.reimbursements.enabled", True)
    activate_policy(world["u"]["founder"], "REIMBURSEMENT_POLICY", POLICY)
    return world


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_employee_claims_with_a_receipt_in_private_storage_and_payroll_approves(claims_world):
    from app.payroll.services import claims

    w = claims_world
    emma, founder = w["u"]["emma"], w["u"]["founder"]
    claim = submit(emma, claim_body(w, "MEALS", "400.00", merchant="Cafe Blue"))
    assert claim["status"] == "SUBMITTED" and claim["approval_request_id"]
    content = pdf("cafe-1")
    r = receipt(emma, claim["id"], content)
    assert r.status_code == 201, r.text
    assert r.json()["sha256"] == hashlib.sha256(content).hexdigest() and r.json()["mime"] == "application/pdf"
    assert (receipt(emma, claim["id"], content).json()["code"]) == "RECEIPT_ALREADY_ATTACHED"
    r = receipt(emma, claim["id"], b"just some text", name="bill.txt")
    assert (r.status_code, r.json()["code"]) == (400, "RECEIPT_TYPE")
    receipt_id = staff_get(founder, claim["id"])["receipts"][0]["id"]
    download = emma.get(f"{MY}/{claim['id']}/receipts/{receipt_id}")
    assert download.status_code == 200 and download.content == content
    assert "no-store" in download.headers["cache-control"]
    [(path,)] = sql("SELECT storage_path FROM reimbursement_receipts WHERE id = :i", i=receipt_id)
    assert "claim-receipts" in path and not path.startswith(("/", "http"))

    assert emma.post(f"{STAFF}/{claim['id']}/approve", json={}).status_code == 403
    r = founder.post(f"{STAFF}/{claim['id']}/approve", json={"remarks": "ok"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    row = staff_get(founder, claim["id"])
    assert row["approved"] == {"amount": "400.00", "currency": "INR"} and row["claim_type_name"] == "Meals"
    assert row["payout_route"] == "WITH_PAYROLL" and row["fx_conversion"] is None
    assert founder.post(f"{STAFF}/{claim['id']}/approve", json={}).status_code == 409
    actions = audit_actions(w["cid"], "reimbursement_claim", claim["id"])
    assert actions[0] == "CLAIM_SUBMITTED" and {"CLAIM_RECEIPT_ADDED", "CLAIM_RECEIPT_DOWNLOADED",
                                                 "CLAIM_APPROVED"} <= set(actions)
    assert sql("SELECT module FROM approval_requests WHERE id = :i", i=claim["approval_request_id"]) == [
        ("reimbursement",)]

    start, end = month_of(w["today"])
    with session(w["cid"]) as (db, _):
        [payable] = claims.claims_for_payroll(db, w["cid"], w["emp"]["Emma"], start, end)
    assert (payable["id"], payable["amount"], payable["currency"], payable["payout_route"]) == (
        claim["id"], "400.00", "INR", "WITH_PAYROLL")
    assert len(payable["version_hash"]) == 64


def test_missing_required_receipt_blocks_the_approval_until_attached(claims_world):
    w = claims_world
    emma, founder = w["u"]["emma"], w["u"]["founder"]
    claim = submit(emma, claim_body(w, "MEALS", "800.00"))
    assert "CLAIM_RECEIPT_REQUIRED" in claim["flags"]
    r = founder.post(f"{STAFF}/{claim['id']}/approve", json={})
    assert (r.status_code, r.json()["code"]) == (409, "CLAIM_RECEIPT_REQUIRED")
    assert staff_get(founder, claim["id"])["status"] == "SUBMITTED"
    assert receipt(emma, claim["id"], pdf("meal-800")).status_code == 201
    assert "CLAIM_RECEIPT_REQUIRED" not in [f["code"] for f in staff_get(founder, claim["id"])["flags"]]
    r = founder.post(f"{STAFF}/{claim['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text


def test_limits_per_claim_and_month_cap_the_approved_amount(claims_world):
    w = claims_world
    emma, founder = w["u"]["emma"], w["u"]["founder"]
    first = submit(emma, claim_body(w, "MEALS", "1200.00"))
    receipt(emma, first["id"], pdf("limit-1"))
    assert "CLAIM_ABOVE_PER_CLAIM_LIMIT" in first["flags"]
    assert founder.post(f"{STAFF}/{first['id']}/approve", json={}).status_code == 200
    assert staff_get(founder, first["id"])["approved"]["amount"] == "1000.00"

    second = submit(emma, claim_body(w, "MEALS", "900.00"))
    receipt(emma, second["id"], pdf("limit-2"))
    assert founder.post(f"{STAFF}/{second['id']}/approve", json={}).status_code == 200
    row = staff_get(founder, second["id"])
    assert row["approved"]["amount"] == "500.00"  # 1,500 a month, 1,000 already approved
    assert "CLAIM_ABOVE_MONTHLY_LIMIT" in [f["code"] for f in row["flags"]]

    third = submit(emma, claim_body(w, "MEALS", "100.00"))
    r = founder.post(f"{STAFF}/{third['id']}/approve", json={})
    assert (r.status_code, r.json()["code"]) == (409, "CLAIM_LIMIT_EXHAUSTED")
    r = founder.post(f"{STAFF}/{third['id']}/reject", json={"remarks": "Monthly limit reached"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    assert staff_get(founder, third["id"])["status_reason"] == "Monthly limit reached"


def test_ec_var_15_taxability_comes_from_the_claim_type_and_never_counts_as_wages(claims_world):
    from app.payroll.services import claims

    w = claims_world
    claim = submit(w["u"]["emma"], claim_body(w, "TRAVEL", "250.00"))
    assert staff_get(w["u"]["founder"], claim["id"])["taxability"] == "EXEMPT"
    w["u"]["founder"].post(f"{STAFF}/{claim['id']}/approve", json={})
    start, end = month_of(w["today"])
    with session(w["cid"]) as (db, _):
        [payable] = claims.claims_for_payroll(db, w["cid"], w["emp"]["Emma"], start, end)
    assert payable["taxability"] == "EXEMPT" and payable["claim_type"] == "TRAVEL"


# ---------------------------------------------------------------------------
# Duplicates and currencies
# ---------------------------------------------------------------------------
def test_ec_var_04_a_receipt_claimed_twice_is_flagged_without_revealing_other_claims(claims_world):
    from app.payroll.time_pay_claims import claim_fingerprint

    w = claims_world
    emma, pia = w["u"]["emma"], w["u"]["pia"]
    content = pdf("taxi-42")
    first = submit(emma, claim_body(w, "TRAVEL", "300.00", merchant="City Taxi"))
    assert receipt(emma, first["id"], content).status_code == 201
    second = submit(emma, claim_body(w, "TRAVEL", "300.00", merchant="  CITY taxi "))
    r = receipt(emma, second["id"], content)
    assert r.status_code == 201, r.text

    staff_view = staff_get(pia, second["id"])
    [flag] = [f for f in staff_view["flags"] if f["code"] == "CLAIM_POSSIBLE_DUPLICATE"]
    assert str(first["id"]) in flag["message"] and flag["severity"] == "WARNING"
    mine = emma.get(f"{MY}/{second['id']}").json()
    [own_flag] = [f for f in mine["flags"] if f["code"] == "CLAIM_POSSIBLE_DUPLICATE"]
    assert own_flag == {"code": "CLAIM_POSSIBLE_DUPLICATE", "severity": "WARNING",
                        "message": "This receipt may already have been claimed."}

    # Another employee's claim with the same file is flagged for the approver as well.
    other = submit(pia, {**claim_body(w, "TRAVEL", "120.00"), "employee_id": w["emp"]["Ivan"]}, url=STAFF)
    assert receipt(pia, other["id"], content, url=STAFF).status_code == 201
    assert "CLAIM_POSSIBLE_DUPLICATE" in [f["code"] for f in staff_get(pia, other["id"])["flags"]]

    digest = hashlib.sha256(content).hexdigest()
    expense = w["today"] - timedelta(days=2)
    stored = {c: f for (c, f) in sql("SELECT claim_id, fingerprint FROM reimbursement_receipts WHERE company_id = :c",
                                      c=w["cid"])}
    assert stored[first["id"]] == stored[second["id"]] == claim_fingerprint(
        digest, Decimal("300.00"), "INR", expense, "City Taxi")
    assert stored[other["id"]] != stored[first["id"]]  # other amount: same receipt, other fingerprint


def test_ec_var_09_foreign_currency_claim_is_converted_at_the_expense_date_and_the_conversion_kept(claims_world):
    w = claims_world
    emma, founder = w["u"]["emma"], w["u"]["founder"]
    expense = w["today"] - timedelta(days=2)
    fx_rate(w["cid"], "USD", "INR", "83.250000", expense)
    claim = submit(emma, claim_body(w, "TRAVEL", "100.00", currency="USD"))
    row = staff_get(founder, claim["id"])
    assert row["amount"] == {"amount": "100.00", "currency": "USD"}
    assert row["payout"] == {"amount": "8325.00", "currency": "INR"}
    conversion = row["fx_conversion"]
    assert conversion["source"] == {"amount": "100.00", "currency": "USD"}
    assert conversion["result"] == {"amount": "8325.00", "currency": "INR"}
    assert Decimal(conversion["rate"]) == Decimal("83.25") and conversion["rate_date"] == expense.isoformat()
    assert (conversion["ref_type"], conversion["ref_id"], conversion["purpose"]) == (
        "reimbursement_claim", claim["id"], "REIMBURSEMENT")
    assert founder.post(f"{STAFF}/{claim['id']}/approve", json={}).status_code == 200
    assert staff_get(founder, claim["id"])["approved"] == {"amount": "8325.00", "currency": "INR"}

    before = sql("SELECT count(*) FROM reimbursement_claims WHERE company_id = :c", c=w["cid"])[0][0]
    r = emma.post(MY, json=claim_body(w, "TRAVEL", "100.00", currency="EUR"))
    assert (r.status_code, r.json()["code"]) == (409, "CLAIM_FX_RATE_MISSING")  # never converted 1:1
    assert sql("SELECT count(*) FROM reimbursement_claims WHERE company_id = :c", c=w["cid"])[0][0] == before
    with tenant(w["cid"]) as conn:  # a foreign-currency claim always names its conversion
        expect_error(conn, CHECK, "UPDATE reimbursement_claims SET fx_conversion_id = NULL WHERE id = :i",
                     i=claim["id"])


# ---------------------------------------------------------------------------
# Payout routes
# ---------------------------------------------------------------------------
def test_separate_payment_claims_are_scheduled_once_and_paid_only_on_evidence(claims_world):
    from app.core.errors import AppError
    from app.payroll.services import claims

    w = claims_world
    cid = w["cid"]
    set_setting(cid, "payroll.reimbursements.payout_route", "SEPARATE_PAYMENT")
    claim = submit(w["u"]["emma"], claim_body(w, "TRAVEL", "210.00"))
    w["u"]["founder"].post(f"{STAFF}/{claim['id']}/approve", json={})
    start, end = month_of(w["today"])
    with session(cid) as (db, _):
        assert claims.claims_for_payroll(db, cid, w["emp"]["Emma"], start, end) == []
        [payable] = claims.claims_for_payment(db, cid, legal_entity_id=w["le01"]["id"])
        assert payable["id"] == claim["id"] and payable["payout_route"] == "SEPARATE_PAYMENT"
        assert claims.claims_for_payment(db, cid, legal_entity_id=w["ae"]["id"]) == []
        assert claims.schedule_claims(db, cid, [claim["id"]], "payment_batch:9") == 1
        assert claims.schedule_claims(db, cid, [claim["id"]], "payment_batch:9") == 0
        db.commit()
        with pytest.raises(AppError) as info:
            claims.schedule_claims(db, cid, [claim["id"]], "payment_batch:10")
        assert info.value.status_code == 409
        db.rollback()
        assert claims.release_claims(db, cid, "payment_batch:9") == 1
        assert claims.claims_for_payment(db, cid)[0]["id"] == claim["id"]
        db.commit()
        with pytest.raises(AppError):
            claims.mark_claims_paid(db, cid, [claim["id"]], "instruction:5")  # not scheduled
        db.rollback()
        claims.schedule_claims(db, cid, [claim["id"]], "payment_batch:11")
        assert claims.mark_claims_paid(db, cid, [claim["id"]], "instruction:5") == 1
        assert claims.mark_claims_paid(db, cid, [claim["id"]], "instruction:5") == 0
        with pytest.raises(ValueError):
            claims.mark_claims_paid(db, cid, [claim["id"]], "")
        db.commit()
    row = staff_get(w["u"]["founder"], claim["id"])
    assert (row["status"], row["paid_ref"]) == ("PAID", "instruction:5")
    assert "CLAIM_PAID" in audit_actions(cid, "reimbursement_claim", claim["id"])
    with tenant(cid) as conn:
        expect_error(conn, CHECK, "UPDATE reimbursement_claims SET paid_ref = NULL WHERE id = :i", i=claim["id"])


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_claim_validation_and_server_owned_fields(claims_world):
    w = claims_world
    emma = w["u"]["emma"]
    r = emma.post(MY, json=claim_body(w, "SPA"))
    assert (r.status_code, r.json()["code"]) == (400, "CLAIM_TYPE_UNKNOWN")
    r = emma.post(MY, json=claim_body(w, days_ago=-3))
    assert (r.status_code, r.json()["code"]) == (400, "EXPENSE_IN_FUTURE")
    data = claim_body(w)
    data["amount"] = {"amount": 300, "currency": "INR"}
    assert emma.post(MY, json=data).status_code == 422  # a JSON number is never money
    assert emma.post(MY, json=claim_body(w, amount="-5.00")).status_code == 422
    assert emma.post(MY, json=claim_body(w, amount="1.001")).status_code == 422
    for extra in ({"status": "APPROVED"}, {"employee_id": w["emp"]["Ivan"]}, {"payout_route": "SEPARATE_PAYMENT"},
                  {"approved_amount": "1.00"}):
        assert emma.post(MY, json=claim_body(w, **extra)).status_code == 422, extra
    set_setting(w["cid"], "payroll.reimbursements.enabled", False)
    r = emma.post(MY, json=claim_body(w))
    assert (r.status_code, r.json()["code"]) == (409, "REIMBURSEMENTS_DISABLED")


# ---------------------------------------------------------------------------
# Scope, self-service and tenants
# ---------------------------------------------------------------------------
def test_ec_sec_05_claimants_see_their_own_and_payroll_staff_their_scope(claims_world):
    w = claims_world
    emma, gus, cleo, pia, beta = (w["u"][k] for k in ("emma", "gus", "cleo", "pia", "beta"))
    mine = submit(emma, claim_body(w, "TRAVEL", "90.00"))
    receipt(emma, mine["id"], pdf("emma-scope"))
    gulf = submit(gus, claim_body(w, "TRAVEL", "40.00", currency="AED"))
    assert staff_get(pia, gulf["id"])["payout"] == {"amount": "40.00", "currency": "AED"}

    assert {c["id"] for c in cleo.get(STAFF).json()["items"]} == {mine["id"]}
    assert cleo.get(f"{STAFF}/{gulf['id']}").status_code == 404
    assert cleo.post(f"{STAFF}/{gulf['id']}/approve", json={}).status_code == 404
    r = cleo.post(STAFF, json={**claim_body(w, "TRAVEL", "10.00", currency="AED"), "employee_id": w["emp"]["Gus"]})
    assert r.status_code == 404, r.text

    assert emma.get(f"{MY}/{gulf['id']}").status_code == 404
    assert gus.get(f"{MY}/{mine['id']}/receipts/1").status_code == 404
    assert [c["id"] for c in emma.get(MY).json()["items"]] == [mine["id"]]
    assert {c["id"] for c in emma.get(STAFF).json()["items"]} == {mine["id"]}  # SELF read only
    r = emma.post(STAFF, json={**claim_body(w), "employee_id": w["emp"]["Ivan"]})
    assert r.status_code == 403, r.text
    assert gus.post(f"{MY}/{mine['id']}/withdraw", json={}).status_code == 404
    r = emma.post(f"{MY}/{mine['id']}/withdraw", json={"remarks": "Wrong trip"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    assert staff_get(pia, mine["id"])["status_reason"] == "Withdrawn by the requester"
    assert emma.post(f"{MY}/{mine['id']}/withdraw", json={}).status_code == 409
    assert receipt(emma, mine["id"], pdf("late")).status_code == 409

    assert cleo.post(f"{STAFF}/{gulf['id']}/reject", json={"remarks": "x"}).status_code == 404
    assert pia.post(f"{STAFF}/{gulf['id']}/approve", json={}).status_code == 200
    assert beta.get(f"{STAFF}/{gulf['id']}").status_code == 404
    assert beta.get(STAFF).json()["total"] == 0
    assert beta.get(f"{MY}/{mine['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Row-level isolation (three tenants)
# ---------------------------------------------------------------------------
TABLES = ("reimbursement_claims", "reimbursement_receipts")


def test_ec_sec_01_claim_tables_are_isolated_across_three_tenants(world):
    from app.models import TENANT_TABLES

    assert set(TABLES) <= set(TENANT_TABLES)
    companies = [world["cid"], world["u"]["beta"].company_id, world["u"]["cobalt"].company_id]
    seeded = {}
    for index, cid in enumerate(companies):
        employee = world["emp"]["Emma"] if index == 0 else create_employee(
            world["u"]["beta" if index == 1 else "cobalt"], "Iso")["id"]
        entity = sql("SELECT id FROM legal_entities WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
        group = world["groups"]["IN"] if index == 0 else pay_group(cid, entity, f"ISO{index}")
        if index:
            place(employee, entity, group)
        version = sql("SELECT id FROM policy_versions WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
        with tenant(cid) as conn:
            claim = conn.execute(text(
                "INSERT INTO reimbursement_claims (company_id, employee_id, legal_entity_id, pay_group_id, "
                "policy_version_id, claim_type, claim_type_name, expense_date, amount, currency, payout_route, "
                "payout_amount, payout_currency) VALUES (:c, :e, :le, :pg, :v, 'X', 'X', '2026-10-01', 10, 'INR', "
                "'WITH_PAYROLL', 10, 'INR') RETURNING id"),
                {"c": cid, "e": employee, "le": entity, "pg": group, "v": version}).scalar()
            rec = conn.execute(text(
                "INSERT INTO reimbursement_receipts (company_id, claim_id, file_name, storage_path, sha256, fingerprint, "
                "mime, bytes) VALUES (:c, :cl, 'r.pdf', 'x', :h, :h, 'application/pdf', 1) RETURNING id"),
                {"c": cid, "cl": claim, "h": "b" * 64}).scalar()
        seeded[cid] = {"reimbursement_claims": claim, "reimbursement_receipts": rec, "employee": employee,
                       "entity": entity, "group": group, "version": version}

    with tenant(None) as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": foreign}).scalar() == 0
                    assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                        {"i": foreign}).rowcount == 0
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": foreign}).rowcount == 0
                expect_error(conn, DENIED, f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0],
                             i=seeded[cid][table])
    a, b = companies[0], companies[1]
    with tenant(a) as conn:  # composite keys refuse another tenant's claim, employee or policy version
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO reimbursement_receipts (company_id, claim_id, file_name, storage_path, sha256, "
                     "fingerprint, mime, bytes) VALUES (:c, :cl, 'r.pdf', 'x', :h, :h, 'application/pdf', 1)",
                     c=a, cl=seeded[b]["reimbursement_claims"], h="c" * 64)
        for column in ("employee", "version"):
            values = {k: seeded[a][k] for k in ("employee", "entity", "group", "version")}
            values[column] = seeded[b][column]
            expect_error(conn, FOREIGN_KEY,
                         "INSERT INTO reimbursement_claims (company_id, employee_id, legal_entity_id, pay_group_id, "
                         "policy_version_id, claim_type, claim_type_name, expense_date, amount, currency, payout_route, "
                         "payout_amount, payout_currency) VALUES (:c, :e, :le, :pg, :v, 'X', 'X', '2026-10-01', 10, "
                         "'INR', 'WITH_PAYROLL', 10, 'INR')",
                         c=a, e=values["employee"], le=values["entity"], pg=values["group"], v=values["version"])
