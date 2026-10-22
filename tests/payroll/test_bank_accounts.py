"""Payee bank accounts: country-driven entry checks, envelope encryption, masking, verification (format,
document review, SANDBOX provider checks), duplicate fingerprints, reveal, payment failures, the read API
for payments and run pre-checks, scope, and row-level isolation of the bank tables across three tenants
(3.7 §2-§5, §12; 3.9 §3.8 and §4 Bank accounts).

EC-BNK-01, EC-BNK-02, EC-BNK-04, EC-BNK-05, EC-BNK-07, EC-BNK-08, EC-BNK-10, EC-BNK-11, EC-BNK-17, EC-SEC-01.

The fixtures and helpers here are shared with test_bank_change_control.py and test_funding_accounts.py.
"""

import email
import json
import re
from email import policy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.config import settings
from tests.helpers import create_employee, invite_and_activate
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    CHECK,
    DENIED,
    FOREIGN_KEY,
    expect_error,
    place,
    role,
    session,
    set_setting,
    sql,
    tenant,
    user_with_role,
    world,
)

STAFF = "/api/v1/payroll/bank-accounts"
CHANGES = "/api/v1/payroll/bank-change-requests"
MY = "/api/v1/me/payroll/bank-accounts"
MY_CHANGES = "/api/v1/me/payroll/bank-change-requests"
IN_NUMBER = "50100123456789"
AE_IBAN = "AE070331234567890123456"
MASK = "••••"
BANK_TABLES = ("bank_accounts", "bank_account_change_requests", "bank_verifications", "funding_accounts")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def payee_url(employee_id, kind="employee"):
    return f"/api/v1/payroll/payees/{kind}/{employee_id}/bank-accounts"


def in_account(holder="Emma Test", number=IN_NUMBER, ifsc="HDFC0001234", **extra):
    return {"holder_name": holder, "country": "IN", "currency": "INR", "account_number": number,
            "routing": {"ifsc": ifsc}, "account_type": "SAVINGS", "bank_name": "HDFC Bank", **extra}


def ae_account(holder="Gus Test", iban=AE_IBAN, **extra):
    return {"holder_name": holder, "country": "AE", "currency": "AED", "iban": iban, "bank_name": "Emirates Bank",
            **extra}


def step_up(client, minutes_ago=0):
    """Mark the client's live sessions as stepped up `minutes_ago` minutes ago (the emailed-code flow itself is
    covered by test_step_up.py)."""
    sql("UPDATE user_sessions SET step_up_at = now() - make_interval(mins => :m), step_up_method = 'EMAIL_OTP' "
        "WHERE user_id = :u AND revoked_at IS NULL", u=user_id(client), m=minutes_ago)


def user_id(client):
    if getattr(client, "user_id", None) is None:
        client.user_id = client.me()["user"]["id"]
    return client.user_id


def submit(client, data, url=MY, expect=201):
    r = client.post(url, json=data)
    assert r.status_code == expect, r.text
    return r.json()


def approve(client, change_id, **body):
    r = client.post(f"{CHANGES}/{change_id}/approve", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def account_row(account_id):
    rows = sql("SELECT row_to_json(b) FROM bank_accounts b WHERE id = :i", i=account_id)
    return rows[0][0]


def change_row(change_id):
    return sql("SELECT row_to_json(c) FROM bank_account_change_requests c WHERE id = :i", i=change_id)[0][0]


def staff_get(client, account_id):
    r = client.get(f"{STAFF}/{account_id}")
    assert r.status_code == 200, r.text
    return r.json()


def usable(company_id, employee_id, on=None):
    from app.core import clock
    from app.payroll.services import bank_accounts

    with session(company_id) as (db, _):
        return bank_accounts.usable_accounts(db, company_id, employee_id, on or clock.utcnow())


def active_account(w, submitter, data, *, url=MY, waive=True, approver="ava"):
    """Submit, approve (waiving the cooling-off by default) and return (account_id, change_id)."""
    step_up(submitter)
    change = submit(submitter, data, url=url)
    step_up(w["u"][approver])
    body = {"waive_cooling_off_reason": "Test: start paying now"} if waive else {}
    result = approve(w["u"][approver], change["id"], **body)
    assert result["status"] == "APPLIED", result
    return change["bank_account_id"], change["id"]


def verify_with_document(w, account_id, owner_client, url=MY, approver="ava"):
    r = owner_client.post(f"{url}/{account_id}/evidence",
                          files={"file": ("cheque.pdf", b"%PDF-1.4\n% cancelled cheque\n%%EOF\n", "application/pdf")})
    assert r.status_code == 201, r.text
    step_up(w["u"][approver])
    r = w["u"][approver].post(f"{STAFF}/{account_id}/verify",
                              json={"method": "DOCUMENT_REVIEW", "decision": "PASS", "evidence_id": r.json()["id"]})
    assert r.status_code == 200, r.text
    return r.json()


def outbox_files(company_id, template="payroll.bank_change_alert"):
    folder = Path(settings.email_outbox_dir).resolve() / str(company_id)
    return sorted(folder.glob(f"{template}-*.eml"), key=lambda p: p.stat().st_mtime_ns) if folder.exists() else []


def mails(company_id, template="payroll.bank_change_alert"):
    """Decoded alert e-mails: [{"to", "subject", "text", "html"}]."""
    out = []
    for path in outbox_files(company_id, template):
        message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        plain = message.get_body(preferencelist=("plain",))
        rich = message.get_body(preferencelist=("html",))
        out.append({"to": str(message["To"]), "subject": str(message["Subject"]),
                    "text": plain.get_content() if plain is not None else "",
                    "html": rich.get_content() if rich is not None else ""})
    return out


def clear_outbox(company_id):
    for path in outbox_files(company_id):
        path.unlink()


def no_plaintext(text, *values):
    return all(v not in text for v in values)


@pytest.fixture
def bank_world(world, app):
    """world (Acme with LE01 India and AE01 UAE, Beta, Cobalt) plus approver Ava (payroll_approver), Rex (reveal),
    Hal (edit + approve), Fay (finance_payments) and Max (masked bank reader for AE01 only); employees Emma, Gus
    (login) and Ivan (no login). Pia (payroll_processor) edits bank details company-wide."""
    acme = world["u"]["founder"]
    le01, ae = world["le01"]["id"], world["ae"]["id"]
    extra = {n: create_employee(acme, n, hire_date="2024-01-15") for n in ("Ava", "Rex", "Hal", "Fay", "Max")}
    for name in ("Ava", "Rex", "Hal", "Fay"):
        place(extra[name]["id"], le01, world["groups"]["IN"])
    place(extra["Max"]["id"], le01, world["groups"]["IN"])
    reveal_role = role(acme, "Bank revealer", "COMPANY",
                       ("payroll:bank_details:read_masked", "payroll:bank_details:read_full"))
    maker_checker = role(acme, "Bank maker-checker", "COMPANY",
                         ("payroll:bank_details:read_masked", "payroll:bank_details:edit",
                          "payroll:bank_details:approve"))
    gulf_reader = role(acme, "Gulf bank reader", "LEGAL_ENTITY", ("payroll:bank_details:read_masked",))
    world["u"].update({
        "ava": invite_and_activate(app, acme, extra["Ava"], ["employee", "payroll_approver"]),
        "rex": user_with_role(app, acme, extra["Rex"], reveal_role),
        "hal": user_with_role(app, acme, extra["Hal"], maker_checker),
        "fay": invite_and_activate(app, acme, extra["Fay"], ["finance_payments"]),
        "max": user_with_role(app, acme, extra["Max"], gulf_reader, [ae]),
    })
    world["emp"].update({k: v["id"] for k, v in extra.items()})
    for key in ("beta", "cobalt"):
        user_id(world["u"][key])
    return world


# ---------------------------------------------------------------------------
# Entry checks (EC-BNK-04, EC-BNK-05) and encryption/masking (EC-BNK-11)
# ---------------------------------------------------------------------------
def test_ec_bnk_04_ec_bnk_05_wrong_country_fields_and_bad_iban_are_rejected_on_entry(bank_world):
    w = bank_world
    gus = w["u"]["gus"]
    step_up(gus)
    r = gus.post(MY, json=ae_account(routing={"ifsc": "HDFC0001234"}))
    assert r.status_code == 422 and r.json()["code"] == "FIELD_NOT_FOR_COUNTRY", r.text
    r = gus.post(MY, json=ae_account(iban="AE080331234567890123456"))
    assert r.status_code == 422 and r.json()["code"] == "IBAN_CHECKSUM", r.text
    assert "AE080331234567890123456" not in r.text
    emma = w["u"]["emma"]
    step_up(emma)
    r = emma.post(MY, json=in_account(iban=AE_IBAN))
    assert r.status_code == 422 and r.json()["code"] == "IBAN_NOT_USED_IN_COUNTRY", r.text
    r = emma.post(MY, json=in_account(ifsc="HDFC1001234"))
    assert r.status_code == 422 and r.json()["code"] == "IFSC_FORMAT" and IN_NUMBER not in r.text
    r = emma.post(MY, json=in_account(currency="XAU"))
    assert r.status_code == 422 and r.json()["code"] == "NON_MONETARY_CURRENCY"
    assert sql("SELECT count(*) FROM bank_accounts WHERE company_id = :c", c=w["cid"])[0][0] == 0
    # A valid UAE IBAN passes the format check, which is recorded but never verifies the account.
    change = submit(gus, ae_account())
    row = account_row(change["bank_account_id"])
    assert (row["status"], row["verification_status"], row["last4"]) == ("PENDING_APPROVAL", "PENDING", "3456")
    assert sql("SELECT method, result FROM bank_verifications WHERE bank_account_id = :a",
               a=row["id"]) == [("FORMAT", "PASSED")]


def test_ec_bnk_11_numbers_are_encrypted_at_rest_and_masked_in_every_output(bank_world):
    w = bank_world
    cid, emma = w["cid"], w["u"]["emma"]
    clear_outbox(cid)
    step_up(emma)
    change = submit(emma, in_account())
    assert set(change) == {"id", "change_request_id", "bank_account_id", "status", "approval_request_id",
                           "row_version"} and change["status"] == "PENDING_APPROVAL"
    row = account_row(change["bank_account_id"])
    assert row["account_number_enc"] and IN_NUMBER not in row["account_number_enc"] and row["iban_enc"] is None
    # A per-tenant keyed HMAC-SHA256 (hex), never derived visibly from the number.
    assert row["last4"] == "6789" and re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"])
    assert IN_NUMBER not in row["fingerprint"]
    own = emma.get(f"{MY}/{row['id']}").json()
    assert own["account_masked"] == f"{MASK}6789" and "risk_flags" not in own and "payment_hold" not in own
    staff = staff_get(w["u"]["pia"], row["id"])
    assert staff["account_masked"] == f"{MASK}6789" and staff["verifications"][0]["method"] == "FORMAT"
    listing = w["u"]["pia"].get(CHANGES).json()
    texts = [json.dumps(own), json.dumps(staff), json.dumps(listing), emma.get(MY).json().__repr__(),
             emma.get(MY_CHANGES).text]
    step_up(w["u"]["ava"])
    assert approve(w["u"]["ava"], change["id"])["status"] == "APPLIED"
    audit = sql("SELECT coalesce(details::text, '') || description FROM audit_logs WHERE company_id = :c", c=cid)
    notes = sql("SELECT title || message FROM notifications WHERE company_id = :c", c=cid)
    sent = [m["subject"] + m["text"] + m["html"] for m in mails(cid)]
    jobs = sql("SELECT payload::text FROM jobs WHERE company_id = :c", c=cid)
    assert sent and notes and all(f"{MASK}6789" in m for m in sent)
    for value in texts + [a for (a,) in audit] + [n for (n,) in notes] + sent + [j for (j,) in jobs]:
        assert no_plaintext(value, IN_NUMBER, "0123456789"), value[:300]
    # The ciphertext is bound to its row and payee: moved to another row it no longer decrypts.
    from app.payroll.services import bank_accounts

    with session(cid) as (db, _):
        clear = bank_accounts.unseal(db, cid, "bank_accounts", row["id"], f"payee=EMPLOYEE:{w['emp']['Emma']}",
                                     row["account_number_enc"], None)
        assert clear["account_number"] == IN_NUMBER
        with pytest.raises(Exception):
            bank_accounts.unseal(db, cid, "bank_accounts", row["id"] + 1, f"payee=EMPLOYEE:{w['emp']['Emma']}",
                                 row["account_number_enc"], None)
        with pytest.raises(Exception):
            bank_accounts.unseal(db, cid, "bank_accounts", row["id"], f"payee=EMPLOYEE:{w['emp']['Ivan']}",
                                 row["account_number_enc"], None)


def test_bank_details_are_immutable_and_never_deleted(bank_world):
    w = bank_world
    account_id, _ = active_account(w, w["u"]["emma"], in_account())
    with tenant(w["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE bank_accounts SET last4 = '0000' WHERE id = :i", i=account_id)
        expect_error(conn, CHECK, "UPDATE bank_accounts SET holder_name = 'Mallory' WHERE id = :i", i=account_id)
        expect_error(conn, DENIED, "DELETE FROM bank_accounts WHERE id = :i", i=account_id)
        expect_error(conn, DENIED, "UPDATE bank_verifications SET result = 'PASSED' WHERE bank_account_id = :i",
                     i=account_id)
    from sqlalchemy.exc import DBAPIError

    for statement in ("DELETE FROM bank_accounts WHERE id = :i",
                      "DELETE FROM bank_verifications WHERE bank_account_id = :i"):
        with pytest.raises(DBAPIError) as info:
            sql(statement, i=account_id)
        assert info.value.orig.pgcode == CHECK


def test_strict_request_models_refuse_server_owned_fields_and_json_numbers(bank_world):
    w = bank_world
    emma = w["u"]["emma"]
    step_up(emma)
    for extra in ({"company_id": 99}, {"status": "ACTIVE"}, {"verification_status": "VERIFIED"},
                  {"allocation": {"type": "PERCENT", "value": 50}}, {"routing": {"ifsc": "HDFC0001234", "x": "1"}}):
        r = emma.post(MY, json={**in_account(), **extra})
        assert r.status_code == 422, (extra, r.text)
    r = emma.post(MY, json={**in_account(), "allocation": {"type": "PERCENT", "value": "50"}})
    assert (r.status_code, r.json()["code"]) == (400, "SPLIT_PAYMENTS_DISABLED")
    assert sql("SELECT count(*) FROM bank_accounts WHERE company_id = :c", c=w["cid"])[0][0] == 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def test_format_never_verifies_and_document_review_is_decided_by_someone_else(bank_world):
    w = bank_world
    emma, ava, pia = w["u"]["emma"], w["u"]["ava"], w["u"]["pia"]
    account_id, _ = active_account(w, emma, in_account())
    row = account_row(account_id)
    assert (row["status"], row["verification_status"]) == ("ACTIVE", "PENDING")
    step_up(ava)
    r = ava.post(f"{STAFF}/{account_id}/verify", json={"method": "FORMAT"})
    assert (r.status_code, r.json()["code"]) == (400, "METHOD_NOT_ALLOWED")
    r = ava.post(f"{STAFF}/{account_id}/verify", json={"method": "PENNY_DROP"})
    assert (r.status_code, r.json()["code"]) == (409, "METHOD_NOT_ENABLED")
    r = ava.post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW", "decision": "PASS"})
    assert (r.status_code, r.json()["code"]) == (400, "EVIDENCE_REQUIRED")
    r = emma.post(f"{MY}/{account_id}/evidence", files={"file": ("x.exe", b"MZ\x90\x00", "application/pdf")})
    assert (r.status_code, r.json()["code"]) == (400, "EVIDENCE_TYPE")
    pdf = b"%PDF-1.4\n% cancelled cheque 6789\n%%EOF\n"
    r = emma.post(f"{MY}/{account_id}/evidence", files={"file": ("../../cheque.pdf", pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    evidence = r.json()
    assert evidence["file_name"] == "cheque.pdf" and "path" not in json.dumps(evidence)
    stored = sql("SELECT evidence_storage_path FROM bank_verifications WHERE id = :i", i=evidence["id"])[0][0]
    assert stored and not stored.startswith("/") and ".." not in stored
    # Payroll staff without approve cannot decide; the employee never verifies their own account.
    step_up(pia)
    r = pia.post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW", "decision": "PASS",
                                                       "evidence_id": evidence["id"]})
    assert r.status_code == 403
    assert pia.get(f"{STAFF}/{account_id}/evidence/{evidence['id']}").status_code == 403
    r = emma.post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW", "decision": "PASS",
                                                        "evidence_id": evidence["id"]})
    assert r.status_code in (403, 404)
    download = ava.get(f"{STAFF}/{account_id}/evidence/{evidence['id']}")
    assert download.status_code == 200 and download.content == pdf
    assert "no-store" in download.headers["cache-control"]
    r = ava.post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW", "decision": "PASS",
                                                       "evidence_id": evidence["id"], "row_version": 1})
    assert (r.status_code, r.json()["code"]) == (409, "ROW_VERSION_CONFLICT")
    result = verify_with_document(w, account_id, emma)
    assert result["verification_status"] == "VERIFIED" and result["verification"]["sandbox"] is False
    row = account_row(account_id)
    assert row["verification_method"] == "DOCUMENT_REVIEW" and row["verified_at"]
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = "
                                 "'bank_account' AND entity_id = :e ORDER BY id", c=w["cid"], e=str(account_id))]
    assert {"BANK_EVIDENCE_UPLOADED", "BANK_EVIDENCE_DOWNLOADED", "BANK_VERIFICATION_RECORDED",
            "BANK_VERIFIED"} <= set(actions)


def test_sandbox_provider_checks_are_labelled_and_refused_in_production(bank_world, monkeypatch):
    w = bank_world
    set_setting(w["cid"], "payroll.bank.verification_methods", ["FORMAT", "PENNY_DROP"])
    ava = w["u"]["ava"]
    good, _ = active_account(w, w["u"]["emma"], in_account())
    step_up(ava)
    r = ava.post(f"{STAFF}/{good}/verify", json={"method": "PENNY_DROP"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification_status"] == "VERIFIED" and body["verification"]["provider"] == "SANDBOX"
    assert body["verification"]["sandbox"] is True and body["verification"]["label"].startswith("SANDBOX")
    assert body["verification"]["name_match_score"] == "1.0000"
    closed, _ = active_account(w, w["u"]["pia"], in_account("Ivan Test", "50100123450000"),
                               url=payee_url(w["emp"]["Ivan"]))
    step_up(ava)
    r = ava.post(f"{STAFF}/{closed}/verify", json={"method": "PENNY_DROP"})
    assert r.json()["verification_status"] == "FAILED" and r.json()["verification"]["result"] == "FAILED"
    third, _ = active_account(w, w["u"]["gus"], ae_account())
    monkeypatch.setattr(settings, "environment", "production")
    step_up(ava)
    r = ava.post(f"{STAFF}/{third}/verify", json={"method": "PENNY_DROP"})
    assert (r.status_code, r.json()["code"]) == (409, "PROVIDER_NOT_CONFIGURED")
    assert account_row(third)["verification_status"] == "PENDING"


def test_ec_bnk_08_holder_name_mismatch_requires_review(bank_world):
    w = bank_world
    pia, ava = w["u"]["pia"], w["u"]["ava"]
    step_up(pia)
    change = submit(pia, in_account("John Smith", "50100123456781"), url=payee_url(w["emp"]["Ivan"]))
    account_id = change["bank_account_id"]
    assert "HOLDER_NAME_MISMATCH" in change["risk_flags"] and "HR_CHANGE" in change["risk_flags"]
    assert account_row(account_id)["verification_status"] == "REQUIRES_REVIEW"
    step_up(ava)
    approve(ava, change["id"], waive_cooling_off_reason="Test only")
    r = ava.post(f"{STAFF}/{account_id}/review", json={"action": "CLEAR_REVIEW"})
    assert (r.status_code, r.json()["code"]) == (400, "REASON_REQUIRED")
    r = pia.post(f"{STAFF}/{account_id}/review", json={"action": "CLEAR_REVIEW", "reason": "Name checked"})
    assert r.status_code == 403
    r = ava.post(f"{STAFF}/{account_id}/review", json={"action": "CLEAR_REVIEW", "reason": "Married name, checked"})
    assert r.status_code == 200 and r.json()["verification_status"] == "PENDING", r.text
    flags = account_row(account_id)["risk_flags"]
    assert flags[0]["code"] == "HOLDER_NAME_MISMATCH" and flags[0]["open"] is False


# ---------------------------------------------------------------------------
# Duplicates (EC-BNK-07) and payment failures (EC-BNK-10)
# ---------------------------------------------------------------------------
def test_ec_bnk_07_the_same_account_on_two_payees_flags_both_and_holds_the_newer(bank_world):
    w = bank_world
    emma_account, _ = active_account(w, w["u"]["emma"], in_account())
    verify_with_document(w, emma_account, w["u"]["emma"])
    ivan_account, _ = active_account(w, w["u"]["pia"], in_account("Ivan Test"), url=payee_url(w["emp"]["Ivan"]))
    older, newer = account_row(emma_account), account_row(ivan_account)
    assert older["fingerprint"] == newer["fingerprint"]
    assert [f["role"] for f in older["risk_flags"] if f["code"] == "DUPLICATE_ACCOUNT"] == ["OLDER"]
    assert [f["role"] for f in newer["risk_flags"] if f["code"] == "DUPLICATE_ACCOUNT"] == ["NEWER"]
    assert (older["verification_status"], older["payment_hold"]) == ("REQUIRES_REVIEW", False)
    assert (newer["verification_status"], newer["payment_hold"], newer["hold_reason"]) == (
        "REQUIRES_REVIEW", True, "DUPLICATE_FINGERPRINT")
    [ivan] = usable(w["cid"], w["emp"]["Ivan"])
    assert ivan["payable"] is False and "HELD_DUPLICATE_FINGERPRINT" in ivan["hold_reasons"]
    [emma] = usable(w["cid"], w["emp"]["Emma"])
    assert emma["payable"] is True and emma["verified"] is True
    detail = staff_get(w["u"]["pia"], ivan_account)
    # The flag never names the other payee or their account (only a generic detail and the role).
    flags = [f for f in detail["risk_flags"] if f["code"] == "DUPLICATE_ACCOUNT"]
    assert flags and all(set(f) <= {"code", "open", "at", "role", "detail"} for f in flags)
    assert f"EMPLOYEE:{w['emp']['Emma']}" not in json.dumps(detail) and IN_NUMBER not in json.dumps(detail)
    held = w["u"]["pia"].get(STAFF, params={"payment_hold": "true"}).json()
    assert [i["id"] for i in held["items"]] == [ivan_account]
    step_up(w["u"]["ava"])
    r = w["u"]["ava"].post(f"{STAFF}/{ivan_account}/review",
                           json={"action": "CLEAR_REVIEW", "reason": "Joint account of spouses, confirmed"})
    assert r.status_code == 200 and r.json()["payment_hold"] is False


def test_ec_bnk_10_a_returned_payment_marks_the_account_failed_and_prompts_the_employee(bank_world):
    from app.payroll.services import bank_accounts

    w = bank_world
    cid, emma = w["cid"], w["u"]["emma"]
    account_id, _ = active_account(w, emma, in_account())
    verify_with_document(w, account_id, emma)
    with session(cid) as (db, _):
        result = bank_accounts.mark_payment_failed(db, account_id, f"R03 account {IN_NUMBER} closed")
        db.commit()
    assert result == {"bank_account_id": account_id, "verification_status": "FAILED", "previous": "VERIFIED"}
    row = account_row(account_id)
    assert row["verification_status"] == "FAILED" and row["verified_at"] is None
    assert IN_NUMBER not in row["failure_reason"]
    own = emma.get(f"{MY}/{account_id}").json()
    assert own["update_required"] is True
    [item] = usable(cid, w["emp"]["Emma"])
    assert item["payable"] is False and "VERIFICATION_FAILED" in item["hold_reasons"]
    [(title, message)] = sql("SELECT title, message FROM notifications WHERE user_id = :u AND title LIKE "
                             "'Payment failed%'", u=emma.user_id)
    assert "update your bank details" in message and f"{MASK}6789" in message and IN_NUMBER not in message
    with session(cid) as (db, _):  # a second return of the same account is recorded, status stays FAILED
        again = bank_accounts.mark_payment_failed(db, account_id, "R03 again")
        db.commit()
    assert again["verification_status"] == "FAILED" and again["previous"] == "FAILED"
    assert sql("SELECT count(*) FROM bank_verifications WHERE bank_account_id = :a AND method = 'PAYMENT_RETURN'",
               a=account_id)[0][0] == 2
    # The employee resubmits: verification starts again (FAILED -> PENDING).
    step_up(emma)
    r = emma.post(f"{STAFF}/{account_id}/review", json={"action": "RESUBMIT"})
    assert r.status_code in (403, 404)
    r = w["u"]["pia"].post(f"{STAFF}/{account_id}/review", json={"action": "RESUBMIT"})
    assert r.status_code == 200 and r.json()["verification_status"] == "PENDING"
    with session(cid) as (db, _):
        with pytest.raises(Exception):
            bank_accounts.mark_payment_failed(db, account_id + 999, "unknown")


# ---------------------------------------------------------------------------
# Reveal
# ---------------------------------------------------------------------------
def test_reveal_needs_read_full_a_reason_and_step_up_and_is_audited_and_rate_limited(bank_world, monkeypatch):
    from app.payroll.services import bank_accounts

    w = bank_world
    cid, rex, pia = w["cid"], w["u"]["rex"], w["u"]["pia"]
    account_id, _ = active_account(w, w["u"]["gus"], ae_account())
    url = f"{STAFF}/{account_id}/reveal"
    step_up(pia)
    assert pia.post(url, json={"reason": "Bank asked to confirm the IBAN"}).status_code == 403
    r = rex.post(url, json={"reason": "Bank asked to confirm the IBAN"})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(rex)
    r = rex.post(url, json={"reason": "because"})
    assert (r.status_code, r.json()["code"]) == (400, "REASON_REQUIRED")
    r = rex.post(url, json={"reason": "Bank asked to confirm the IBAN"})
    assert r.status_code == 200, r.text
    assert r.json()["iban"] == AE_IBAN and r.json()["account_masked"] == f"AE{MASK}3456"
    assert "no-store" in r.headers["cache-control"]
    [(details,)] = sql("SELECT details::text FROM audit_logs WHERE company_id = :c AND action = "
                       "'BANK_DETAILS_REVEALED'", c=cid)
    assert "Bank asked to confirm the IBAN" in details and AE_IBAN not in details
    assert rex.post(f"{STAFF}/{account_id + 999}/reveal", json={"reason": "Bank asked to confirm"}).status_code == 404
    monkeypatch.setattr(bank_accounts, "REVEAL_LIMIT_PER_HOUR", 2)
    assert rex.post(url, json={"reason": "Second check for the bank"}).status_code == 200
    r = rex.post(url, json={"reason": "Third check for the bank"})
    assert (r.status_code, r.json()["code"]) == (429, "RATE_LIMITED")
    beta = w["u"]["beta"]
    step_up(beta)
    assert beta.post(url, json={"reason": "Cross-tenant attempt here"}).status_code in (403, 404)


# ---------------------------------------------------------------------------
# Read API for payments and pre-checks (EC-BNK-01, EC-BNK-02)
# ---------------------------------------------------------------------------
def test_ec_bnk_01_ec_bnk_02_read_api_history_and_run_prechecks(bank_world):
    from app.core import clock
    from app.payroll.services import bank_accounts
    from app.payroll.services import runs as run_service

    w = bank_world
    cid, emma = w["cid"], w["u"]["emma"]
    first, _ = active_account(w, emma, in_account())
    verify_with_document(w, first, emma)
    before_change = clock.utcnow()
    step_up(emma)
    change = submit(emma, in_account(number="50100123456782"))
    second = change["bank_account_id"]
    assert [a["bank_account_id"] for a in usable(cid, w["emp"]["Emma"])] == [first]  # pending: not listed
    step_up(w["u"]["ava"])
    approve(w["u"]["ava"], change["id"], remarks="Checked with the employee by phone")
    now = clock.utcnow()
    items = usable(cid, w["emp"]["Emma"], now)
    assert [(i["bank_account_id"], i["payable"]) for i in items] == [(first, True), (second, False)]
    assert items[1]["hold_reasons"][0] == "COOLING_OFF" and items[0]["encrypted"]["account_number"]
    assert set(items[0]) >= {"account_masked", "holder_name", "routing", "allocation", "snapshot_hash", "verified"}
    after = now + timedelta(hours=49)
    assert [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"], after)] == [second]
    assert [i["bank_account_id"] for i in usable(cid, w["emp"]["Emma"], before_change)] == [first]
    with session(cid) as (db, _):
        engine_items = bank_accounts.engine_bank_accounts(db, cid, w["emp"]["Emma"], now)
        assert [e["account_id"] for e in engine_items] == [first] and engine_items[0]["type"] == "PRIMARY"
        assert bank_accounts.changed_since(db, cid, w["emp"]["Emma"], before_change) is True
        assert bank_accounts.changed_since(db, cid, w["emp"]["Gus"], before_change) is False
        snapshot = usable(cid, w["emp"]["Emma"], now)[0]
        assert bank_accounts.beneficiary_plaintext(db, cid, snapshot)["account_number"] == IN_NUMBER
        findings = bank_accounts.precheck_findings(db, cid, [w["emp"]["Emma"], w["emp"]["Ivan"]], now,
                                                   require_verification=True)
        assert [(f["employee_id"], f["code"]) for f in findings] == [(w["emp"]["Ivan"], "MISSING_BANK")]
        findings = bank_accounts.precheck_findings(db, cid, [w["emp"]["Emma"]], after, require_verification=True)
        assert [f["code"] for f in findings] == ["UNVERIFIED_BANK"]
        assert bank_accounts.precheck_findings(db, cid, [w["emp"]["Emma"]], after, require_verification=False) == []
        # The run pre-check hook turns them into WARNING findings per run employee.
        # A pay date means its first instant (company timezone): the day after `after` is past the cooling-off.
        period = SimpleNamespace(period_start=now.date(), period_end=now.date(),
                                 pay_date=(after + timedelta(days=1)).date())
        ctx = SimpleNamespace(db=db, run=SimpleNamespace(company_id=cid), group=SimpleNamespace(id=w["groups"]["IN"]),
                              period=period, included=[SimpleNamespace(id=501, employee_id=w["emp"]["Emma"]),
                                                       SimpleNamespace(id=502, employee_id=w["emp"]["Ivan"])])
        found = run_service.bank_account_findings(ctx)
    assert sorted((f.code, f.severity, f.run_employee_id) for f in found) == [
        ("MISSING_BANK", "WARNING", 502), ("UNVERIFIED_BANK", "WARNING", 501)]
    assert all(not f.blocks_finalization for f in found)


# ---------------------------------------------------------------------------
# Scope and isolation (EC-SEC-01)
# ---------------------------------------------------------------------------
def test_scope_self_service_and_payee_routes(bank_world):
    w = bank_world
    gus_account, _ = active_account(w, w["u"]["gus"], ae_account())
    emma_account, _ = active_account(w, w["u"]["emma"], in_account())
    emma, cleo, max_, pia = w["u"]["emma"], w["u"]["cleo"], w["u"]["max"], w["u"]["pia"]
    assert emma.get(f"{MY}/{gus_account}").status_code == 404
    assert [i["id"] for i in emma.get(MY).json()["items"]] == [emma_account]
    assert cleo.get(STAFF).status_code == 403  # no bank permission at all
    assert cleo.get(f"{STAFF}/{emma_account}").status_code == 403
    assert [i["id"] for i in max_.get(STAFF).json()["items"]] == [gus_account]  # AE01 only
    assert max_.get(f"{STAFF}/{emma_account}").status_code == 404
    assert max_.get(payee_url(w["emp"]["Emma"])).status_code == 404
    assert [i["id"] for i in max_.get(payee_url(w["emp"]["Gus"])).json()["items"]] == [gus_account]
    assert pia.get(payee_url(w["emp"]["Gus"], "contractor")).status_code == 404
    assert pia.get(payee_url(w["emp"]["Gus"], "vendor")).status_code == 422
    assert pia.get(payee_url(999999)).status_code == 404
    step_up(max_)
    assert max_.post(payee_url(w["emp"]["Gus"]), json=ae_account()).status_code == 403  # read only
    step_up(pia)
    assert pia.post(payee_url(999999), json=in_account()).status_code == 404
    assert pia.post(payee_url(w["emp"]["Gus"], "nominee"), json=ae_account()).status_code == 404
    # A replacement can only name one of the payee's own active accounts.
    r = pia.post(payee_url(w["emp"]["Gus"]), json={**ae_account(), "replaces_account_id": emma_account})
    assert r.status_code == 404
    # Self-service switched off: employees cannot change their account (HR still can).
    set_setting(w["cid"], "payroll.bank.self_service_enabled", False)
    step_up(emma)
    r = emma.post(MY, json=in_account(number="50100123456783"))
    assert (r.status_code, r.json()["code"]) == (409, "BANK_SELF_SERVICE_DISABLED")


def test_ec_sec_01_bank_tables_are_isolated_across_three_tenants(bank_world):
    w = bank_world
    cid = w["cid"]
    beta, cobalt = w["u"]["beta"], w["u"]["cobalt"]
    account_id, change_id = active_account(w, w["u"]["emma"], in_account())
    for client in (beta, cobalt):
        step_up(client)
        assert client.get(f"{STAFF}/{account_id}").status_code in (403, 404)
        assert client.get(f"{CHANGES}/{change_id}").status_code in (403, 404)
        assert client.get(payee_url(w["emp"]["Emma"])).status_code in (403, 404)
        r = client.get(STAFF)
        assert r.status_code == 403 or r.json()["items"] == []
        r = client.post(payee_url(w["emp"]["Emma"]), json=in_account())
        assert r.status_code in (403, 404)
        assert client.post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW"}).status_code in (403,
                                                                                                             404)
        assert client.post(f"{CHANGES}/{change_id}/approve", json={}).status_code in (403, 404)
        assert client.get(f"{MY}/{account_id}").status_code in (403, 404)
    beta_id, cobalt_id = beta.company_id, cobalt.company_id
    for other in (beta_id, cobalt_id):
        with tenant(other) as conn:
            for table in BANK_TABLES:
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0
            # Inserting a row for another tenant fails RLS; a row of this tenant pointing at Acme's employee or
            # account fails the composite same-company foreign keys.
            expect_error(conn, DENIED, "INSERT INTO bank_verifications (company_id, bank_account_id, method, result, "
                         "details, at) VALUES (:c, :a, 'MANUAL_REVIEW', 'CLEARED', '{}', now())", c=cid, a=account_id)
            expect_error(conn, FOREIGN_KEY, "INSERT INTO bank_verifications (company_id, bank_account_id, method, "
                         "result, details, at) VALUES (:c, :a, 'MANUAL_REVIEW', 'CLEARED', '{}', now())",
                         c=other, a=account_id)
            expect_error(conn, FOREIGN_KEY, "INSERT INTO bank_accounts (company_id, payee_type, payee_id, employee_id, "
                         "holder_name, country, currency, account_number_enc, last4, fingerprint, status, "
                         "verification_status) VALUES (:c, 'EMPLOYEE', :e, :e, 'X', 'IN', 'INR', 'x', '1234', 'fp', "
                         "'DRAFT', 'NOT_SUBMITTED')", c=other, e=w["emp"]["Emma"])
            expect_error(conn, FOREIGN_KEY, "INSERT INTO funding_accounts (company_id, legal_entity_id, name, "
                         "holder_name, country, currency, account_number_enc, fingerprint, signatory_user_ids) VALUES "
                         "(:c, :le, 'X', 'X', 'IN', 'INR', 'x', 'fp', ARRAY[1])", c=other, le=w["le01"]["id"])
        with tenant(other) as conn:
            updated = conn.execute(text(
                "UPDATE bank_accounts SET status_reason = 'hijack' WHERE id = :i"), {"i": account_id})
            assert updated.rowcount == 0
    with tenant(cid) as conn:
        assert conn.execute(text("SELECT count(*) FROM bank_accounts")).scalar() == 1
    assert account_row(account_id)["status_reason"] != "hijack"


def test_ec_bnk_11_only_reveal_and_bank_file_functions_decrypt_bank_details():
    """Audit 07 §10: decryption is confined to the reveal and bank-file read functions (AST allow-list)."""
    import ast

    root = Path(__file__).resolve().parents[2] / "app" / "payroll" / "services"
    allowed = {
        "bank_accounts.py": {"decrypt_field": {"unseal"}, "unseal": {"reveal_account", "beneficiary_plaintext"}},
        "funding_accounts.py": {"decrypt_field": set(), "unseal": {"reveal", "funding_plaintext"}},
    }
    for name, rules in allowed.items():
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in [n for n in ast.walk(function) if isinstance(n, ast.Call)]:
                target = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
                if target in rules:
                    assert function.name in rules[target], f"{name}:{function.name} calls {target}"
