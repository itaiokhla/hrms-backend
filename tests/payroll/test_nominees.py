"""Nominees and legal heirs as payees of a deceased employee's final settlement (3.5 §15, 3.7 §2.1).

EC-BNK-15 (deceased employee: the payee changes to the nominee with documents and approval), EC-BNK-14 (an
account in a currency the rail cannot pay is not payable), EC-BNK-16 (format and directory checks on a nominee's
account, the employee flow's), EC-COR-07 (death pays the nominee as a separate payee), EC-SEC-01 (isolation).

The world is the final-settlement one (Acme: Ivan is terminated; the founder is the payroll manager, Pia the
processor who edits bank details, Ava the approver). Pia is the maker of nominees' accounts, Ava the checker.
"""

import csv
import io
from decimal import ROUND_DOWN, Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.helpers import create_employee
from tests.payroll.test_bank_accounts import (  # noqa: F401
    CHANGES,
    IN_NUMBER,
    MASK,
    STAFF,
    in_account,
    step_up,
)
from tests.payroll.test_compensation import login_as
from tests.payroll.test_final_settlement import (  # noqa: F401  (fs_world and its fixtures)
    CASES,
    case,
    fs_world,
    open_case,
    retro_world,  # noqa: F401  (fixture used by fs_world)
    runs_world,  # noqa: F401  (fixture used by fs_world)
    settle_items,
    terminate,
)
from tests.payroll.test_payment_obligations import add_bank_account, add_funding_account
from tests.payroll.test_retro import calculate, finalize
from tests.payroll.test_wage_inputs import (
    DENIED,
    FOREIGN_KEY,
    expect_error,
    role,
    session,
    set_setting,
    sql,
    tenant,
    user_with_role,
)

NOM = "/api/v1/payroll/nominees"
BATCHES = "/api/v1/payroll/payment-batches"
PROOF = "Succession certificate SC/2026/114"
MARK = "••••"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def nom_world(fs_world, app):
    w = fs_world
    w["app"] = app
    w["maker"], w["checker"] = w["processor"], w["approver"]
    w["emma_id"] = w["emma"]
    return w


def death_case(w, *, settle=True, employee=None):
    employee = employee or w["ivan"]
    created = open_case(w["acme"], employee, w["last_day"], termination_type="DEATH",
                        payee={"type": "NOMINEE", "name": "Asha Nair", "relationship": "Spouse",
                               "evidence_ref": "Nomination form 2"})
    data = case(w["acme"], created["id"])
    return settle_items(w["acme"], data) if settle else data


def make_nominee(client, case_id, name, share, *, relationship="Spouse", expect=201, proof=PROOF, country="IN",
                 kind="NOMINEE", **extra):
    step_up(client)
    r = client.post(NOM, json={"final_settlement_case_id": case_id, "name": name, "share_percent": str(share),
                               "relationship": relationship, "proof_ref": proof, "country": country, "kind": kind,
                               **extra})
    assert r.status_code == expect, r.text
    return r.json()


def nominee_account(client, nominee_id, holder, number=IN_NUMBER, *, expect=201, ifsc="HDFC0001234", **extra):
    step_up(client)
    r = client.post(f"{NOM}/{nominee_id}/bank-accounts", json=in_account(holder=holder, number=number, ifsc=ifsc,
                                                                         **extra))
    assert r.status_code == expect, r.text
    return r.json()


def approve_change(client, change_id, expect=200, **body):
    step_up(client)
    r = client.post(f"{CHANGES}/{change_id}/approve", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def verify(w, account_id):
    """Document review: the maker uploads the cheque, the checker passes it."""
    step_up(w["maker"])
    r = w["maker"].post(f"{STAFF}/{account_id}/evidence",
                        files={"file": ("cheque.pdf", b"%PDF-1.4\n% cancelled cheque\n%%EOF\n", "application/pdf")})
    assert r.status_code == 201, r.text
    step_up(w["checker"])
    r = w["checker"].post(f"{STAFF}/{account_id}/verify", json={"method": "DOCUMENT_REVIEW", "decision": "PASS",
                                                                "evidence_id": r.json()["id"]})
    assert r.status_code == 200, r.text


def age(account_id):
    """The cooling-off is over (time passes; nobody waives it)."""
    sql("UPDATE bank_accounts SET approved_at = now() - interval '60 hours', usable_from = now() - interval '12 hours' "
        "WHERE id = :a", a=account_id)


def ready_account(w, nominee_id, holder, number=IN_NUMBER, **kw):
    """A nominee account submitted by the maker, approved by the checker, past its cooling-off and verified."""
    change = nominee_account(w["maker"], nominee_id, holder, number, **kw)
    approve_change(w["checker"], change["id"])
    verify(w, change["bank_account_id"])
    age(change["bank_account_id"])
    return change["bank_account_id"]


def account_row(account_id):
    return sql("SELECT row_to_json(b) FROM bank_accounts b WHERE id = :i", i=account_id)[0][0]


def instructions(run_id, include_closed=False):
    clause = "" if include_closed else "AND i.status NOT IN ('CANCELLED', 'REISSUED') "
    return sql("SELECT i.id, i.status, i.status_reason, i.amount, i.beneficiary_snapshot, i.bank_account_id "
               "FROM payment_instructions i WHERE i.run_id = :r " + clause + "ORDER BY i.id", r=run_id)


def settle_all(w, case_id):
    """Decide every checklist item (the salary of the exit period is prorated in the settlement run, so the run
    has a positive net to divide)."""
    return settle_items(w["acme"], case(w["acme"], case_id))


def run_settlement(w, case_id):
    r = w["acme"].post(f"{CASES}/{case_id}/run", json={})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    for (input_id,) in sql("SELECT id FROM wage_inputs WHERE company_id = :c AND employee_id = :e AND "
                           "status = 'PENDING_APPROVAL' ORDER BY id", c=w["cid"], e=w["ivan"]):
        r = w["checker"].post(f"/api/v1/payroll/wage-inputs/{input_id}/approve", json={})
        assert r.status_code == 200, r.text
    calculate(w["acme"], run_id)
    finalize(w, run_id)
    return run_id


def net_of(run_id, employee_id):
    return sql("SELECT net FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e", r=run_id,
               e=employee_id)[0][0]


# ---------------------------------------------------------------------------
# Records: validation, scope, step-up, audit (EC-BNK-15, EC-COR-07)
# ---------------------------------------------------------------------------
def test_ec_bnk_15_nominee_records_are_validated_scoped_stepped_up_and_audited(nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    assert data["nominees"] == {"active": 0, "share_total": "0", "complete": False, "editable": True,
                                "issues": [{"code": "NO_NOMINEES", "message": "at least one nominee must receive "
                                                                              "the settlement"}]}
    body = {"final_settlement_case_id": data["id"], "name": "Asha Nair", "relationship": "Spouse",
            "proof_ref": PROOF, "share_percent": "60", "country": "IN"}
    # A change of identity needs a fresh step-up; an approver without edit rights is refused.
    r = w["maker"].post(NOM, json=body)
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(w["checker"])
    assert w["checker"].post(NOM, json=body).status_code == 403  # approve is not edit
    # Strict validation.
    step_up(w["maker"])
    for bad, code in (({"share_percent": "0"}, "SHARE_RANGE"), ({"share_percent": "100.01"}, "SHARE_RANGE"),
                      ({"share_percent": "10.1234567"}, "SHARE_PRECISION"), ({"country": "ZZZ"}, None),
                      ({"country": "QQ"}, "COUNTRY_INVALID"), ({"proof_ref": "x"}, "TEXT_REQUIRED"),
                      ({"name": " "}, None), ({"kind": "FRIEND"}, None), ({"email": "not-mail"}, "EMAIL_INVALID"),
                      ({"relationship": "S\u0000pouse"}, None)):
        r = w["maker"].post(NOM, json={**body, **bad})
        assert r.status_code in (400, 422), (bad, r.text)
        if code:
            assert r.json()["code"] == code, (bad, r.text)
    r = w["maker"].post(NOM, json={**body, "share_percent": 60})  # a JSON number in a money-like field
    assert r.status_code == 422
    r = w["maker"].post(NOM, json={**body, "status": "ACTIVE"})  # server-owned fields are refused
    assert r.status_code == 422
    r = w["maker"].post(NOM, json={**body, "final_settlement_case_id": 99999})
    assert r.status_code == 404
    first = make_nominee(w["maker"], data["id"], "Asha Nair", "60", email="Asha.Nair@Example.Test")
    assert first["status"] == "ACTIVE" and first["row_version"] == 1
    make_nominee(w["maker"], data["id"], "Asha Nair", "10", expect=409)  # DUPLICATE_NOMINEE
    r = w["maker"].post(NOM, json={**body, "name": "Bram Nair", "relationship": "Son", "share_percent": "40.000001"})
    assert r.status_code == 409 and r.json()["code"] == "SHARES_EXCEED"
    second = make_nominee(w["maker"], data["id"], "Bram Nair", "40", relationship="Son", kind="LEGAL_HEIR")
    listing = w["acme"].get(f"{NOM}/cases/{data['id']}").json()
    assert listing["summary"]["complete"] is True and listing["summary"]["share_total"] == "100"
    assert [(n["name"], n["share_percent"], n["kind"]) for n in listing["items"]] == [
        ("Asha Nair", "60", "NOMINEE"), ("Bram Nair", "40", "LEGAL_HEIR")]
    assert listing["items"][0]["email_masked"] == "a***@example.test"  # the e-mail address is masked on reads
    # Update: a share change keeps the 100 rule; a stale row_version is a conflict.
    step_up(w["maker"])
    r = w["maker"].patch(f"{NOM}/{second['id']}", json={"share_percent": "45"})
    assert r.status_code == 409 and r.json()["code"] == "SHARES_EXCEED"
    step_up(w["maker"])
    r = w["maker"].patch(f"{NOM}/{second['id']}", json={"proof_ref": "Court order 77/2026", "row_version": 5})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    step_up(w["maker"])
    r = w["maker"].patch(f"{NOM}/{second['id']}", json={"proof_ref": "Court order 77/2026", "row_version": 1})
    assert r.status_code == 200 and r.json()["row_version"] == 2, r.text
    # A non-death case has no nominees.
    terminate(w["emma_id"], w["last_day"])
    resignation = open_case(w["acme"], w["emma_id"], w["last_day"])
    step_up(w["maker"])
    r = w["maker"].post(NOM, json={**body, "final_settlement_case_id": resignation["id"]})
    assert r.status_code == 409 and r.json()["code"] == "NOT_A_DEATH_CASE"
    # Scope: a bank-details editor of the other legal entity, the clerk without bank rights and the other tenants.
    gwen = create_employee(w["acme"], "Gwen", hire_date="2024-01-15")
    gulf = role(w["acme"], "Gulf bank editor", "LEGAL_ENTITY", ("payroll:bank_details:read_masked",
                                                                "payroll:bank_details:edit"))
    gwen_client = user_with_role(w["app"], w["acme"], gwen, gulf, [w["ae"]])
    step_up(gwen_client)
    assert gwen_client.get(f"{NOM}/{first['id']}").status_code == 404
    assert gwen_client.get(f"{NOM}/cases/{data['id']}").status_code == 404
    assert gwen_client.get(NOM).json() == {"total": 0, "items": []}
    assert gwen_client.post(NOM, json=body).status_code == 404
    assert gwen_client.patch(f"{NOM}/{first['id']}", json={"share_percent": "55"}).status_code == 404
    assert w["cleo"].get(f"{NOM}/{first['id']}").status_code == 403
    for other in (w["beta"]["client"], w["cobalt"]["client"]):
        step_up(other)
        assert other.get(f"{NOM}/{first['id']}").status_code in (403, 404)
        assert other.get(f"{NOM}/cases/{data['id']}").status_code in (403, 404)
        assert other.post(f"{NOM}/{first['id']}/end", json={"reason": "Not theirs to end"}).status_code in (403, 404)
    # End a nominee (reason, step-up); the shares then no longer add up, so the run is blocked.
    step_up(w["maker"], minutes_ago=6)
    r = w["maker"].post(f"{NOM}/{second['id']}/end", json={"reason": "Renounced the share in writing"})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(w["maker"])
    r = w["maker"].post(f"{NOM}/{second['id']}/end", json={"reason": "Renounced the share in writing"})
    assert r.status_code == 200 and r.json()["status"] == "ENDED"
    step_up(w["maker"])
    assert w["maker"].patch(f"{NOM}/{second['id']}", json={"share_percent": "40"}).status_code == 409
    after = case(w["acme"], data["id"])
    assert after["nominees"]["complete"] is False and after["nominees"]["issues"][0]["code"] == "SHARES_INCOMPLETE"
    # Audit: names masked, no proof text, no e-mail.
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'nominee' "
                                 "ORDER BY id", c=cid)]
    assert actions == ["NOMINEE_ADDED", "NOMINEE_ADDED", "NOMINEE_UPDATED", "NOMINEE_ENDED"]
    audit = " ".join(d for (d,) in sql("SELECT details::text || description FROM audit_logs WHERE company_id = :c "
                                        "AND entity_type = 'nominee'", c=cid))
    assert "Asha Nair" not in audit and "Succession" not in audit and "example.test" not in audit.lower()
    assert sql("SELECT count(*) FROM audit_logs WHERE company_id = :c AND action = 'NOMINEE_ADDED' AND "
               "actor_id = :u", c=cid, u=w["maker"].user_id)[0][0] == 2


# ---------------------------------------------------------------------------
# Bank accounts: maker-checker, cooling-off, checks, duplicates, masking (EC-BNK-15, EC-BNK-16)
# ---------------------------------------------------------------------------
def test_ec_bnk_15_a_nominee_account_needs_a_second_person_verification_and_its_cooling_off(nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    # The founder records the nominee; Pia submits the account; neither of them approves it.
    asha = make_nominee(w["acme"], data["id"], "Asha Nair", "100")
    change = nominee_account(w["maker"], asha["id"], "Asha Nair")
    account = account_row(change["bank_account_id"])
    assert (account["payee_type"], account["payee_id"], account["employee_id"], account["nominee_id"]) == (
        "NOMINEE", asha["id"], None, asha["id"])
    assert account["status"] == "PENDING_APPROVAL" and account["last4"] == "6789"
    assert account["account_number_enc"] and IN_NUMBER not in account["account_number_enc"]
    assert change["status"] == "PENDING_APPROVAL" and set(change) >= {"id", "bank_account_id", "risk_flags"}
    assert "NOMINEE_PAYEE" in change["risk_flags"]
    detail = w["checker"].get(f"{CHANGES}/{change['id']}").json()  # the approver sees who the payee is and the proof
    assert detail["nominee"]["proof_ref"] == PROOF and detail["nominee"]["share_percent"] == "100"
    assert detail["after"]["account_masked"] == f"{MASK}6789" and IN_NUMBER not in str(detail)
    assert detail["after"]["nominee_id"] == asha["id"]
    approve_change(w["maker"], change["id"], expect=403)  # the submitter
    r = approve_change(w["acme"], change["id"], expect=403)  # the creator of the nominee record is a maker too
    step_up(w["checker"])
    r = w["checker"].post(f"{CHANGES}/{change['id']}/approve",
                          json={"waive_cooling_off_reason": "The family needs the money now"})
    assert (r.status_code, r.json()["code"]) == (409, "COOLING_OFF_NOT_WAIVABLE")
    other_tenant = w["beta"]["client"]
    step_up(other_tenant)
    assert other_tenant.post(f"{CHANGES}/{change['id']}/approve", json={}).status_code in (403, 404)
    result = approve_change(w["checker"], change["id"])
    assert result["status"] == "APPLIED"
    account = account_row(change["bank_account_id"])
    hours = sql("SELECT extract(epoch FROM usable_from - approved_at) / 3600 FROM bank_accounts WHERE id = :a",
                a=account["id"])[0][0]
    assert account["status"] == "ACTIVE" and round(hours) == 48 and account["approved_by_user_id"] == w["checker"].user_id
    # The cooling-off cannot be waived afterwards either.
    step_up(w["checker"])
    r = w["checker"].post(f"{STAFF}/{account['id']}/waive-cooling-off", json={"reason": "Urgent request by the family"})
    assert (r.status_code, r.json()["code"]) == (409, "COOLING_OFF_NOT_WAIVABLE")
    # Until it is over and verified, the account is listed but never payable.
    from app.payroll.services import bank_accounts

    with session(cid) as (db, _):
        [listed] = bank_accounts.usable_accounts(db, cid, asha["id"], sql("SELECT now()")[0][0],
                                                 payee_type="NOMINEE")
    assert listed["payable"] is False and "COOLING_OFF" in listed["hold_reasons"] and "UNVERIFIED" in listed["hold_reasons"]
    # Masked everywhere, never the number: responses, audit, notifications, jobs.
    mine = w["maker"].get(f"{NOM}/{asha['id']}").json()
    assert mine["bank_accounts"][0]["account_masked"] == f"{MASK}6789" and mine["has_usable_account"] is False
    audit = " ".join(a for (a,) in sql("SELECT coalesce(details::text, '') || description FROM audit_logs "
                                        "WHERE company_id = :c", c=cid))
    notes = " ".join(n for (n,) in sql("SELECT title || message FROM notifications WHERE company_id = :c", c=cid))
    jobs = " ".join(j for (j,) in sql("SELECT payload::text FROM jobs WHERE company_id = :c", c=cid))
    for blob in (str(mine), str(detail), audit, notes, jobs):
        assert IN_NUMBER not in blob and "0123456789" not in blob
    assert f"{MASK}6789" in notes and "settlement payee" in notes.lower()
    # Document verification by someone other than the maker makes it payable after the cooling-off.
    verify(w, account["id"])
    age(account["id"])
    mine = w["maker"].get(f"{NOM}/{asha['id']}").json()
    assert mine["has_usable_account"] is True and mine["bank_accounts"][0]["verification_status"] == "VERIFIED"
    # Reveal needs read_full, a reason and a step-up (the employee flow's) and is audited.
    rex = user_with_role(w["app"], w["acme"], create_employee(w["acme"], "Rex", hire_date="2024-01-15"),
                         role(w["acme"], "Bank revealer", "COMPANY",
                              ("payroll:bank_details:read_masked", "payroll:bank_details:read_full")))
    r = rex.post(f"{STAFF}/{account['id']}/reveal", json={"reason": "Checking the cheque against the account"})
    assert (r.status_code, r.json()["code"]) == (403, "STEP_UP_REQUIRED")
    step_up(rex)
    r = rex.post(f"{STAFF}/{account['id']}/reveal", json={"reason": "Checking the cheque against the account"})
    assert r.status_code == 200 and r.json()["account_number"] == IN_NUMBER, r.text
    assert sql("SELECT count(*) FROM audit_logs WHERE company_id = :c AND action = 'BANK_DETAILS_REVEALED'",
               c=cid)[0][0] == 1
    # A replacement account goes through the same flow; the old one serves until the new one is usable.
    second = nominee_account(w["maker"], asha["id"], "Asha Nair", "50100123456784")
    approve_change(w["checker"], second["id"])
    old, new = account_row(change["bank_account_id"]), account_row(second["bank_account_id"])
    assert old["status"] == "INACTIVE" and old["ended_at"] == new["usable_from"]
    assert old["replaced_by_account_id"] == new["id"] and new["status"] == "ACTIVE"


def test_ec_bnk_08_ec_bnk_07_name_mismatch_and_duplicate_accounts_are_flagged_and_held(nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    asha = make_nominee(w["maker"], data["id"], "Asha Nair", "50")
    bram = make_nominee(w["maker"], data["id"], "Bram Nair", "50", relationship="Son")
    mismatch = nominee_account(w["maker"], asha["id"], "Zed Unrelated Person", "50100123456700")
    row = account_row(mismatch["bank_account_id"])
    assert any(f["code"] == "HOLDER_NAME_MISMATCH" and f["open"] for f in row["risk_flags"])
    assert row["verification_status"] == "REQUIRES_REVIEW"
    # The same account for two payees flags both and holds the newer one (EC-BNK-07), also against the deceased's own.
    own = add_bank_account(cid, w["ivan"], IN_NUMBER, holder="Ivan Test")
    dup = nominee_account(w["maker"], bram["id"], "Bram Nair", IN_NUMBER)
    flagged = {r[0]: r for r in sql("SELECT id, payment_hold, hold_reason, risk_flags::text FROM bank_accounts "
                                    "WHERE id IN (:a, :b, :c)", a=own, b=dup["bank_account_id"],
                                    c=mismatch["bank_account_id"])}
    assert flagged[dup["bank_account_id"]][1] is True and flagged[dup["bank_account_id"]][2] == "DUPLICATE_FINGERPRINT"
    assert "DUPLICATE_ACCOUNT" in flagged[own][3] and "DUPLICATE_ACCOUNT" in flagged[dup["bank_account_id"]][3]
    # Wrong-country fields and a bad IFSC are refused with the employee flow's codes (EC-BNK-04, EC-BNK-16).
    step_up(w["maker"])
    r = w["maker"].post(f"{NOM}/{asha['id']}/bank-accounts", json=in_account(holder="Asha Nair", iban="AE070331234567890123456"))
    assert r.status_code == 422 and r.json()["code"] == "IBAN_NOT_USED_IN_COUNTRY"
    step_up(w["maker"])
    r = w["maker"].post(f"{NOM}/{asha['id']}/bank-accounts", json=in_account(holder="Asha Nair", ifsc="HDFC1001234"))
    assert r.status_code == 422 and r.json()["code"] == "IFSC_FORMAT" and IN_NUMBER not in r.text
    # An ended nominee takes no new account.
    step_up(w["maker"])
    assert w["maker"].post(f"{NOM}/{bram['id']}/end", json={"reason": "Wrong person recorded"}).status_code == 409  # open change
    approve_change(w["checker"], dup["id"])
    step_up(w["maker"])
    assert w["maker"].post(f"{NOM}/{bram['id']}/end", json={"reason": "Wrong person recorded"}).status_code == 200
    assert account_row(dup["bank_account_id"])["status"] == "INACTIVE"  # the account stops serving with its nominee
    nominee_account(w["maker"], bram["id"], "Bram Nair", "50100123456784", expect=409)


# ---------------------------------------------------------------------------
# Payment: obligation, instructions per share, rounding, statement, bank file (EC-BNK-15, EC-COR-07)
# ---------------------------------------------------------------------------
def test_ec_cor_07_ec_bnk_15_settlement_is_divided_among_nominees_paid_to_their_verified_accounts_and_statemented(
        nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    data = settle_all(w, data["id"])
    assert data["status"] == "READY"
    # The run is blocked until the nominees are complete.
    r = w["acme"].post(f"{CASES}/{data['id']}/run", json={})
    assert (r.status_code, r.json()["code"]) == (409, "NOMINEES_INCOMPLETE")
    a = make_nominee(w["maker"], data["id"], "Asha Nair", "33.333333")
    b = make_nominee(w["maker"], data["id"], "Bram Nair", "33.333333", relationship="Son")
    r = w["acme"].post(f"{CASES}/{data['id']}/run", json={})  # 66.666666 percent so far
    assert r.status_code == 409 and r.json()["code"] == "NOMINEES_INCOMPLETE", r.text
    c = make_nominee(w["maker"], data["id"], "Chitra Nair", "33.333334", relationship="Daughter", kind="LEGAL_HEIR")
    # 33.333333 + 33.333333 + 33.333334 = 100: the run is allowed; the deceased's own account must stay unused.
    own = add_bank_account(cid, w["ivan"], "501009876543", holder="Ivan Test")
    accounts = {
        "a": ready_account(w, a["id"], "Asha Nair", "50100123456701"),
        "b": ready_account(w, b["id"], "Bram Nair", "50100123456702"),
        "c": ready_account(w, c["id"], "Chitra Nair", "50100123456703"),
    }
    # Shares are frozen from the moment the run exists.
    run_id = run_settlement(w, data["id"])
    step_up(w["maker"])
    assert w["maker"].patch(f"{NOM}/{a['id']}", json={"share_percent": "30"}).status_code == 409
    make_nominee(w["maker"], data["id"], "Dev Nair", "1", relationship="Nephew", expect=409)
    step_up(w["maker"])
    r = w["maker"].post(f"{NOM}/{a['id']}/end", json={"reason": "Too late to change the shares"})
    assert r.status_code == 409 and r.json()["code"] == "CASE_LOCKED"
    assert case(w["acme"], data["id"])["status"] == "SETTLED"
    net = net_of(run_id, w["ivan"])
    assert net > 0
    [obligation] = sql("SELECT id, payee_type, amount, status, employee_id FROM payment_obligations WHERE run_id = :r",
                       r=run_id)
    assert (obligation[1], obligation[2], obligation[4]) == ("NOMINEE", net, w["ivan"])
    rows = instructions(run_id)
    assert len(rows) == 3 and all(r[1] == "READY" for r in rows), rows
    by_nominee = {r[4]["nominee_id"]: r for r in rows}
    # Hand-computed: the first two shares round down to the paisa, the largest share (the third) takes the rest.
    floor = lambda pct: (net * Decimal(pct) / 100).quantize(Decimal("0.01"), rounding=ROUND_DOWN)  # noqa: E731
    expected = {a["id"]: floor("33.333333"), b["id"]: floor("33.333333")}
    expected[c["id"]] = net - expected[a["id"]] - expected[b["id"]]
    assert {n: Decimal(r[3]) for n, r in by_nominee.items()} == expected
    assert sum(Decimal(r[3]) for r in rows) == net  # exactly the settlement, never more or less
    assert all(r[4]["payee_type"] == "NOMINEE" and r[5] in accounts.values() and r[5] != own for r in rows)
    assert by_nominee[a["id"]][4]["account_masked"] == f"{MASK}6701" and "50100123456701" not in str(rows)
    assert by_nominee[c["id"]][4]["holder_name_masked"].startswith("C")
    # The deceased employee's own (verified, usable) account was never used.
    assert all(r[5] != own for r in rows)
    assert sql("SELECT count(*) FROM payment_instructions WHERE run_id = :r AND bank_account_id = :a",
               r=run_id, a=own)[0][0] == 0
    # Split recorded in the audit trail (names masked, amounts per nominee).
    [split] = sql("SELECT details::text FROM audit_logs WHERE company_id = :c AND action = 'NOMINEE_SETTLEMENT_SPLIT'",
                  c=cid)
    assert "Asha Nair" not in split[0] and f"{expected[c['id']]:.2f}" in split[0]
    # The case page shows the payments per nominee, masked.
    payments = w["acme"].get(f"{NOM}/cases/{data['id']}/payments").json()
    [ob] = payments["obligations"]
    assert ob["amount"]["amount"] == f"{net:.2f}" and len(ob["instructions"]) == 3
    assert {i["share_percent"] for i in ob["instructions"]} == {"33.333333", "33.333334"}
    # One statement per nominee through the documents service, never published to the employee's portal.
    statements = {n: w["acme"].get(f"{NOM}/{n}/statements").json()["items"] for n in (a["id"], b["id"], c["id"])}
    assert all(len(s) == 1 and s[0]["status"] == "GENERATED" for s in statements.values())
    doc = w["acme"].get(f"/api/v1/payroll/documents/{statements[c['id']][0]['document_id']}")
    assert doc.status_code == 200, doc.text
    shown = str(doc.json())
    assert "Chitra Nair" in shown and "Succession" in shown and "50100123456703" not in shown
    assert sql("SELECT subject_type, doc_type, employee_id FROM documents WHERE id = :d",
               d=statements[c["id"]][0]["document_id"])[0] == ("PAYEE", "FINAL_SETTLEMENT_STATEMENT", w["ivan"])
    again = w["acme"].post(f"{NOM}/{c['id']}/statements", json={})  # idempotent: the same document
    assert again.status_code == 201 and again.json()["items"][0]["document_id"] == statements[c["id"]][0]["document_id"]
    # The money path: the finance team batches the three instructions and the bank file names the nominees.
    pete = login_as(w["app"], w["acme"], create_employee(w["acme"], "Pete", "Preparer"), ["finance_payments"])
    alma = login_as(w["app"], w["acme"], create_employee(w["acme"], "Alma", "Approver"), ["finance_payments"])
    fran = login_as(w["app"], w["acme"], create_employee(w["acme"], "Fran", "Finance"), ["finance_payments"])
    add_funding_account(cid, w["le01"], [fran.user_id])
    batch = pete.post(BATCHES, json={"run_id": run_id})
    assert batch.status_code == 201, batch.text
    batch = batch.json()["batches"][0]
    assert alma.post(f"{BATCHES}/{batch['id']}/approve").status_code == 200
    assert pete.post(f"{BATCHES}/{batch['id']}/generate-file").status_code == 200
    file = pete.get(f"{BATCHES}/{batch['id']}/file")
    assert file.status_code == 200
    text_ = file.content.decode("utf-8")
    details = [r for r in csv.reader(io.StringIO(text_)) if r and r[0] == "D"]
    assert len(details) == 3
    for number in ("50100123456701", "50100123456702", "50100123456703"):
        assert number in text_
    assert "501009876543" not in text_  # the deceased's own account
    assert "Chitra Nair" in text_
    assert f"{expected[c['id']]:.2f}" in text_
    # The employee's own My Payments never shows a settlement paid to nominees.
    from app.payroll.services import obligations

    with session(cid) as (db, _):
        class P:  # a principal-like object for the self-service query
            company_id = cid
            employee_id = w["ivan"]
        assert obligations.my_payments(db, P) == {"items": [], "years": []}


def test_nominee_payments_fail_closed_without_account_verification_or_complete_shares(nom_world):
    w = nom_world
    cid = w["cid"]
    # Verification is required for a nominee whatever the company setting says (EC-BNK-15).
    set_setting(cid, "payroll.bank.exclude_unverified_from_payout", False)
    data = death_case(w, settle=False)
    data = settle_all(w, data["id"])
    a = make_nominee(w["maker"], data["id"], "Asha Nair", "70")
    b = make_nominee(w["maker"], data["id"], "Bram Nair", "30", relationship="Son")
    own = add_bank_account(cid, w["ivan"], "501009876543", holder="Ivan Test")
    change = nominee_account(w["maker"], a["id"], "Asha Nair", "50100123456701")
    approve_change(w["checker"], change["id"])
    age(change["bank_account_id"])  # past the cooling-off, but never verified
    run_id = run_settlement(w, data["id"])
    rows = {r[4]["nominee_id"]: r for r in instructions(run_id)}
    net = net_of(run_id, w["ivan"])
    assert rows[a["id"]][1] == "ON_HOLD" and rows[a["id"]][2] == "UNVERIFIED_ACCOUNT"
    # Bram has no account: his share waits, and the deceased employee's own account is not a fallback.
    assert rows[b["id"]][1] == "NOT_READY" and rows[b["id"]][2] == "MISSING_BANK_ACCOUNT" and rows[b["id"]][5] is None
    assert Decimal(rows[a["id"]][3]) + Decimal(rows[b["id"]][3]) == net
    assert all(r[5] != own for r in rows.values())
    # Once Bram's account is approved, verified and past its cooling-off, refresh readiness replaces the
    # placeholder with an instruction of exactly his share (not the whole net).
    change_b = nominee_account(w["maker"], b["id"], "Bram Nair", "50100123456702")
    approve_change(w["checker"], change_b["id"])
    verify(w, change_b["bank_account_id"])
    age(change_b["bank_account_id"])
    from app.payroll.services import obligations
    pete = login_as(w["app"], w["acme"], create_employee(w["acme"], "Pete", "Preparer"), ["finance_payments"])
    with session(cid, pete.user_id) as (db, principal):
        obligations.refresh_readiness(db, principal, run_id)
        db.commit()
    live = {r[4]["nominee_id"]: r for r in instructions(run_id)}
    assert live[b["id"]][1] == "READY" and live[b["id"]][5] == change_b["bank_account_id"]
    assert Decimal(live[b["id"]][3]) == Decimal(rows[b["id"]][3])
    assert live[a["id"]][1] == "ON_HOLD"
    assert sum(Decimal(r[3]) for r in live.values()) == net


def test_nominee_obligation_without_complete_shares_is_one_not_ready_placeholder(nom_world):
    """If the nominees were changed behind the service (database edit), the obligation never pays the deceased
    employee's account and never guesses a split: one NOT_READY placeholder."""
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    data = settle_all(w, data["id"])
    a = make_nominee(w["maker"], data["id"], "Asha Nair", "100")
    ready_account(w, a["id"], "Asha Nair", "50100123456701")
    own = add_bank_account(cid, w["ivan"], "501009876543", holder="Ivan Test")
    r = w["acme"].post(f"{CASES}/{data['id']}/run", json={})
    assert r.status_code == 201, r.text
    run_id = r.json()["id"]
    sql("UPDATE nominees SET status = 'ENDED', ended_at = now(), ended_reason = 'x', ended_by_user_id = :u "
        "WHERE id = :n", u=w["maker"].user_id, n=a["id"])
    calculate(w["acme"], run_id)
    finalize(w, run_id)
    rows = instructions(run_id)
    assert len(rows) == 1 and rows[0][1] == "NOT_READY" and rows[0][5] is None and rows[0][5] != own
    assert rows[0][4]["payee_type"] == "NOMINEE" and rows[0][4]["nominee_issue"] == "NO_NOMINEES"
    assert Decimal(rows[0][3]) == net_of(run_id, w["ivan"])


def test_a_cancelled_case_ends_its_nominees_and_deactivates_their_accounts(nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    a = make_nominee(w["maker"], data["id"], "Asha Nair", "60")
    b = make_nominee(w["maker"], data["id"], "Bram Nair", "40", relationship="Son")
    live = ready_account(w, a["id"], "Asha Nair", "50100123456701")
    pending = nominee_account(w["maker"], b["id"], "Bram Nair", "50100123456702")
    r = w["acme"].post(f"{CASES}/{data['id']}/cancel", json={"reason": "Recorded against the wrong employee"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert {row[0]: row[1] for row in sql("SELECT id, status FROM nominees WHERE company_id = :c", c=cid)} == {
        a["id"]: "ENDED", b["id"]: "ENDED"}
    assert account_row(live)["status"] == "INACTIVE"
    assert account_row(pending["bank_account_id"])["status"] == "DRAFT"
    assert sql("SELECT status FROM bank_account_change_requests WHERE id = :c", c=pending["id"])[0][0] == "CANCELLED"
    step_up(w["maker"])
    r = w["maker"].post(f"{NOM}/{a['id']}/bank-accounts", json=in_account(holder="Asha Nair", number="50100123456709"))
    assert r.status_code == 409 and r.json()["code"] == "NOMINEE_ENDED"


# ---------------------------------------------------------------------------
# Isolation and database rules (EC-SEC-01)
# ---------------------------------------------------------------------------
def test_ec_sec_01_nominees_are_isolated_across_three_tenants_and_guarded_by_the_database(nom_world):
    w = nom_world
    cid = w["cid"]
    data = death_case(w, settle=False)
    a = make_nominee(w["maker"], data["id"], "Asha Nair", "60")
    b = make_nominee(w["maker"], data["id"], "Bram Nair", "40", relationship="Son")
    change = nominee_account(w["maker"], a["id"], "Asha Nair")
    for tenant_world in (w["beta"], w["cobalt"]):
        client = tenant_world["client"]
        step_up(client)
        assert client.get(f"{NOM}/{a['id']}").status_code in (403, 404)
        assert client.get(f"{NOM}/{a['id']}/bank-accounts").status_code in (403, 404)
        assert client.get(f"{STAFF}/{change['bank_account_id']}").status_code in (403, 404)
        assert client.get(f"{CHANGES}/{change['id']}").status_code in (403, 404)
        assert client.post(f"{STAFF}/{change['bank_account_id']}/reveal", json={"reason": "Looking around the tenants"}
                           ).status_code in (403, 404)
        assert client.post(f"{NOM}/{a['id']}/bank-accounts", json=in_account(holder="Asha Nair")).status_code in (403, 404)
        r = client.get(NOM)
        assert r.status_code == 403 or r.json()["items"] == []
        r = client.get(f"{NOM}/bank-change-requests")
        assert r.status_code == 403 or r.json()["items"] == []
        for other in (tenant_world["cid"],):
            with tenant(other) as conn:
                assert conn.execute(text("SELECT count(*) FROM nominees")).scalar() == 0
                expect_error(conn, DENIED, "INSERT INTO nominees (company_id, employee_id, final_settlement_case_id, "
                             "name, relationship, proof_ref, share_percent, country, created_by_user_id) VALUES "
                             "(:c, :e, :k, 'X Y', 'Spouse', 'Proof 1', 10, 'IN', :u)", c=cid, e=w["ivan"], k=data["id"],
                             u=w["maker"].user_id)
                expect_error(conn, FOREIGN_KEY, "INSERT INTO nominees (company_id, employee_id, "
                             "final_settlement_case_id, name, relationship, proof_ref, share_percent, country, "
                             "created_by_user_id) VALUES (:c, :e, :k, 'X Y', 'Spouse', 'Proof 1', 10, 'IN', 1)",
                             c=other, e=w["ivan"], k=data["id"])
                assert conn.execute(text("UPDATE nominees SET share_percent = 1 WHERE id = :i"),
                                    {"i": a["id"]}).rowcount == 0
    assert sql("SELECT share_percent FROM nominees WHERE id = :i", i=a["id"])[0][0] == Decimal("60")
    # A nominee account of another tenant's nominee: composite key refuses it.
    with tenant(w["beta"]["cid"]) as conn:
        expect_error(conn, FOREIGN_KEY, "INSERT INTO bank_accounts (company_id, payee_type, payee_id, nominee_id, "
                     "holder_name, country, currency, account_number_enc, last4, fingerprint, status, "
                     "verification_status) VALUES (:c, 'NOMINEE', :n, :n, 'X', 'IN', 'INR', 'x', '1234', 'fp', "
                     "'DRAFT', 'NOT_SUBMITTED')", c=w["beta"]["cid"], n=a["id"])
    # Database rules: shares above 100, a nominee id on an employee account, deletes, reopening, frozen links.
    with pytest.raises(DBAPIError) as info:  # the deferred shares rule: 60 + 70 > 100
        sql("UPDATE nominees SET share_percent = 70 WHERE id = :i", i=b["id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError) as info:
        sql("DELETE FROM nominees WHERE id = :i", i=b["id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError) as info:
        sql("UPDATE nominees SET employee_id = :e WHERE id = :i", e=w["emma_id"], i=b["id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError) as info:  # payee type and nominee id go together
        sql("UPDATE bank_accounts SET nominee_id = NULL WHERE id = :a", a=change["bank_account_id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError) as info:
        sql("UPDATE bank_accounts SET nominee_id = :n WHERE id = :a", n=b["id"], a=change["bank_account_id"])
    assert info.value.orig.pgcode == "23514"
    sql("UPDATE nominees SET status = 'ENDED', ended_at = now(), ended_reason = 'x', ended_by_user_id = :u "
        "WHERE id = :i", u=w["maker"].user_id, i=b["id"])
    with pytest.raises(DBAPIError) as info:
        sql("UPDATE nominees SET status = 'ACTIVE', ended_at = NULL, ended_reason = NULL, ended_by_user_id = NULL "
            "WHERE id = :i", i=b["id"])
    assert info.value.orig.pgcode == "23514"
    # Nominee accounts never appear in an employee's own lists, and the employee list endpoints do not leak them.
    r = w["checker"].get(f"{STAFF}", params={"employee_id": w["ivan"]})
    assert r.status_code == 200 and r.json()["items"] == []
