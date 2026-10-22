"""Statement import and reconciliation (3.7 §9-§10; 3.5 §5.10; decision I-17; F-09, F-11; EC-PAY-04,
EC-PAY-11, EC-PAY-12, EC-MON-18)."""

import json
from datetime import date, timedelta
from decimal import Decimal

from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import (
    BATCHES,
    MY_PAYMENTS,
    NUMBERS,
    err,
    finalized_run,
    instructions_of,
    ok,
    pay,  # noqa: F401  (fixture)
    run_status,
    submitted_batch,
)
from tests.helpers import create_employee
from tests.payroll.test_wage_inputs import audit_actions, sql

PAY = "/api/v1/payroll"
IMPORT = f"{PAY}/bank-statements/import"
MATCHES = f"{PAY}/reconciliation-matches"
MAPPING = {"date": "Value Date", "amount": "Amount", "currency": "Currency", "reference": "Reference",
           "counterparty": "Beneficiary", "counterparty_account": "Account", "date_format": "%Y-%m-%d",
           "thousands_separator": None}


def csv_statement(*lines):
    """lines: (value_date, signed amount, reference, account) -> CSV bytes (negative = debit)."""
    rows = ["Value Date,Amount,Currency,Reference,Beneficiary,Account"]
    for day, amount, reference, account in lines:
        rows.append(f"{day},{amount},INR,{reference},Payee,{account}")
    return ("\r\n".join(rows) + "\r\n").encode("utf-8")


def upload(client, funding_id, content, *, fmt="CSV", mapping=MAPPING, name="statement.csv", status=201):
    data = {"funding_account_id": str(funding_id), "format": fmt}
    if mapping is not None and fmt == "CSV":
        data["mapping"] = json.dumps(mapping)
    response = client.post(IMPORT, data=data, files={"file": (name, content, "text/csv")})
    assert response.status_code == status, response.text
    return response.json()


def refs(run_id):
    return {r[0]: (r[1], r[2]) for r in sql("SELECT employee_id, id, reference FROM payment_instructions "
                                              "WHERE run_id = :r AND status NOT IN ('CANCELLED', 'REISSUED')",
                                              r=run_id)}


def status_of(instruction_id):
    return sql("SELECT status, evidence_status, evidence_source, variance_amount, variance_reason FROM "
               "payment_instructions WHERE id = :i", i=instruction_id)[0]


def test_statement_match_settles_reconciles_and_reconciles_the_run(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    batch = submitted_batch(pay, run_id)
    today = date.today().isoformat()
    r = refs(run_id)
    emma_id, emma_ref = r[pay["emp"]["Emma"]["id"]]
    ivan_id, ivan_ref = r[pay["emp"]["Ivan"]["id"]]
    content = csv_statement((today, "-50000.00", emma_ref, ""), (today, "-42000.50", ivan_ref, ""),
                            (today, "250000.00", "FUNDING", ""))
    result = upload(pay["fran"], pay["funding"], content)
    assert result["duplicate"] is False and result["import"]["new_line_count"] == 3
    assert result["import"]["counts"] == {"MATCHED": 2, "IGNORED": 1}
    for iid in (emma_id, ivan_id):
        assert status_of(iid)[:3] == ("RECONCILED", "VERIFIED", "STATEMENT")
    assert sql("SELECT status FROM payment_batches WHERE id = :b", b=batch["id"])[0][0] == "RECONCILED"
    assert run_status(run_id) == "RECONCILED"
    assert "STATEMENT_IMPORTED" in audit_actions(pay["cid"], "bank_statement_import", result["import"]["id"])
    emma = login_as(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])
    label = ok(emma.get(MY_PAYMENTS))["items"][0]["instructions"][0]["label"]["text"]
    assert label == f"Paid on {date.today().isoformat()}" or label.startswith("Paid on ")
    listed = ok(pay["fran"].get(f"{PAY}/bank-statements"))
    assert listed["total"] == 1
    matches = ok(pay["fran"].get(f"{PAY}/bank-statements/{result['import']['id']}/matches"))["items"]
    assert {m["status"] for m in matches} == {"MATCHED", "IGNORED"}
    assert NUMBERS["Emma"] not in json.dumps(matches)


def test_f11_reimport_is_idempotent_by_file_and_by_line(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    submitted_batch(pay, run_id)
    r = refs(run_id)
    today = date.today().isoformat()
    first = csv_statement((today, "-100.00", r[pay["emp"]["Emma"]["id"]][1], ""))
    one = upload(pay["fran"], pay["funding"], first)
    again = upload(pay["fran"], pay["funding"], first)
    assert again["duplicate"] is True and again["import"]["id"] == one["import"]["id"]
    # A later statement repeats yesterday's line and adds one new line: only the new one is stored.
    later = csv_statement((today, "-100.00", r[pay["emp"]["Emma"]["id"]][1], ""),
                          (today, "-200.00", r[pay["emp"]["Ivan"]["id"]][1], ""))
    two = upload(pay["fran"], pay["funding"], later)
    assert (two["import"]["line_count"], two["import"]["new_line_count"]) == (2, 1)
    assert sql("SELECT count(*) FROM bank_statement_lines WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 2
    assert run_status(run_id) == "RECONCILED"


def test_ec_pay_11_ec_mon_18_amount_difference_is_never_auto_settled_and_accepted_with_a_reason(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid, ref = refs(run_id)[pay["emp"]["Emma"]["id"]]
    result = upload(pay["fran"], pay["funding"], csv_statement((date.today().isoformat(), "-90.00", ref, "")))
    (match,) = result["matches"]
    assert (match["status"], match["reason"]) == ("MISMATCH", "AMOUNT_DIFFERS")
    assert match["variance"] == {"amount": "-10.00", "currency": "INR"}
    assert status_of(iid)[0] == "SUBMITTED"  # never settled automatically
    queue = ok(pay["fran"].get(MATCHES, params={"queue": "mismatch"}))
    assert [m["id"] for m in queue["items"]] == [match["id"]]
    err(pay["fran"].post(f"{MATCHES}/{match['id']}/accept", json={"reason": ""}), 422)
    err(pay["fran"].post(f"{MATCHES}/{match['id']}/accept", json={"reason": "Bank fee", "row_version": 99}), 409,
        "ROW_VERSION_CONFLICT")
    accepted = ok(pay["fran"].post(f"{MATCHES}/{match['id']}/accept", json={"reason": "Bank fee deducted"}))
    assert accepted["status"] == "ACCEPTED_VARIANCE"
    status, evidence, source, variance, reason = status_of(iid)
    assert (status, evidence, source) == ("RECONCILED", "VERIFIED", "STATEMENT")
    assert variance == Decimal("-10.0000") and reason == "Bank fee deducted"
    assert "MISMATCH_RESOLVED" in audit_actions(pay["cid"], "payment_instruction", iid)
    assert run_status(run_id) == "RECONCILED"
    err(pay["fran"].post(f"{MATCHES}/{match['id']}/accept", json={"reason": "Again"}), 409, "NOT_A_VARIANCE")


def test_ec_pay_12_unknown_debit_goes_to_the_unmatched_queue_and_can_be_escalated(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    result = upload(pay["fran"], pay["funding"], csv_statement((date.today().isoformat(), "-777.00", "UNKNOWN", "")))
    (match,) = result["matches"]
    assert match["status"] == "UNMATCHED" and match["instruction"] is None
    assert [m["id"] for m in ok(pay["fran"].get(MATCHES, params={"queue": "unmatched"}))["items"]] == [match["id"]]
    escalated = ok(pay["fran"].post(f"{MATCHES}/{match['id']}/escalate", json={"reason": "Ask the bank"}))
    assert escalated["status"] == "ESCALATED"
    assert ok(pay["fran"].get(MATCHES, params={"queue": "unmatched"}))["total"] == 0
    assert ok(pay["fran"].get(MATCHES, params={"queue": "escalated"}))["total"] == 1
    assert "RECONCILIATION_ESCALATED" in audit_actions(pay["cid"], "reconciliation_match", match["id"])


def test_duplicate_debit_is_flagged_as_a_possible_double_payment(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid, ref = refs(run_id)[pay["emp"]["Emma"]["id"]]
    today, yesterday = date.today(), date.today() - timedelta(days=1)
    result = upload(pay["fran"], pay["funding"], csv_statement((yesterday.isoformat(), "-100.00", ref, ""),
                                                               (today.isoformat(), "-100.00", ref, "")))
    assert [m["status"] for m in result["matches"]] == ["MATCHED", "DUPLICATE"]
    assert status_of(iid)[0] == "RECONCILED"
    duplicate = result["matches"][1]
    err(pay["fran"].post(f"{MATCHES}/{duplicate['id']}/link", json={"instruction_id": iid, "reason": "Same"}), 409,
        "ALREADY_MATCHED")


def test_ec_pay_04_return_credit_reopens_the_obligation(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid, ref = refs(run_id)[pay["emp"]["Emma"]["id"]]
    today = date.today().isoformat()
    upload(pay["fran"], pay["funding"], csv_statement((today, "-100.00", ref, "")))
    assert status_of(iid)[0] == "RECONCILED" and run_status(run_id) == "RECONCILED"
    result = upload(pay["fran"], pay["funding"], csv_statement((today, "100.00", ref, "")), name="returns.csv")
    (match,) = result["matches"]
    assert match["status"] == "RETURN" and match["instruction"]["id"] == iid
    assert status_of(iid)[0] == "RETURNED"
    assert sql("SELECT status FROM payment_obligations WHERE id = (SELECT obligation_id FROM payment_instructions "
               "WHERE id = :i)", i=iid)[0][0] == "OPEN"
    assert run_status(run_id) == "PARTIALLY_PAID"
    assert sql("SELECT verification_status FROM bank_accounts WHERE employee_id = :e",
               e=pay["emp"]["Emma"]["id"])[0][0] == "FAILED"


def test_unmatched_line_linked_to_the_right_payment_settles_and_reconciles_it(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "100.00"})
    submitted_batch(pay, run_id)
    r = refs(run_id)
    emma_id = r[pay["emp"]["Emma"]["id"]][0]
    result = upload(pay["fran"], pay["funding"], csv_statement((date.today().isoformat(), "-100.00", "SAL OCT", "")))
    (match,) = result["matches"]
    assert match["status"] == "UNMATCHED"
    err(pay["fran"].post(f"{MATCHES}/{match['id']}/link", json={"instruction_id": 999999, "reason": "Emma"}), 404)
    linked = ok(pay["fran"].post(f"{MATCHES}/{match['id']}/link", json={"instruction_id": emma_id,
                                                                        "reason": "Confirmed by the bank"}))
    assert linked["status"] == "LINKED"
    assert status_of(emma_id)[:3] == ("RECONCILED", "VERIFIED", "STATEMENT")
    assert run_status(run_id) == "PARTIALLY_PAID"
    assert "RECONCILIATION_LINKED" in audit_actions(pay["cid"], "reconciliation_match", match["id"])


def test_fallback_match_by_exact_amount_account_and_date_window(pay):
    run_id = finalized_run(pay, {"Emma": "123.45"})
    submitted_batch(pay, run_id)
    iid, _ = refs(run_id)[pay["emp"]["Emma"]["id"]]
    sql("UPDATE payment_instructions SET expected_settlement_date = :d WHERE id = :i", d=date.today(), i=iid)
    content = csv_statement((date.today().isoformat(), "-123.45", "SALARY", NUMBERS["Emma"]))
    (match,) = upload(pay["fran"], pay["funding"], content)["matches"]
    assert (match["status"], match["reason"]) == ("MATCHED", "AMOUNT_ACCOUNT_DATE")
    assert match["line"]["counterparty_account"].endswith("4567") and NUMBERS["Emma"] not in json.dumps(match)
    assert sql("SELECT counterparty_account_masked FROM bank_statement_lines")[0][0] != NUMBERS["Emma"]
    assert status_of(iid)[0] == "RECONCILED"


def test_overdue_queue_lists_sent_payments_past_their_expected_date(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid, _ = refs(run_id)[pay["emp"]["Emma"]["id"]]
    sql("UPDATE payment_instructions SET expected_settlement_date = :d WHERE id = :i",
        d=date.today() - timedelta(days=14), i=iid)
    items = ok(pay["fran"].get(f"{PAY}/reconciliation/overdue"))["items"]
    assert [i["id"] for i in items] == [iid] and items[0]["business_days_overdue"] >= 5
    assert ok(pay["fran"].get(f"{PAY}/reconciliation/overdue", params={"grace": 30}))["items"] == []


def test_i17_partially_paid_run_reconciles_with_carried_items(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    submitted_batch(pay, run_id)
    r = refs(run_id)
    emma_id, emma_ref = r[pay["emp"]["Emma"]["id"]]
    ivan_id, ivan_ref = r[pay["emp"]["Ivan"]["id"]]
    today = date.today().isoformat()
    upload(pay["fran"], pay["funding"], csv_statement((today, "-100.00", emma_ref, "")))
    assert run_status(run_id) == "PARTIALLY_PAID"
    # Ivan's payment is still in flight: nothing can be carried yet.
    err(pay["fran"].post(f"{PAY}/runs/{run_id}/payments/reconcile", json={"reason": "Month end"}), 409,
        "PAYMENTS_IN_FLIGHT")
    from tests.payroll.test_payment_events import evidence

    evidence(pay["cid"], ivan_id, "FAILED", source="STATEMENT", event_id="bank-report-1", reason_code="AC04")
    err(pay["fran"].post(f"{PAY}/runs/{run_id}/payments/reconcile", json={}), 400, "REASON_REQUIRED")
    reconciled = ok(pay["fran"].post(f"{PAY}/runs/{run_id}/payments/reconcile",
                                     json={"reason": "Month end: Ivan's payment carried for reissue"}))
    assert reconciled["status"] == "RECONCILED" and run_status(run_id) == "RECONCILED"
    assert reconciled["carried"] == [{"instruction_id": ivan_id, "status": "FAILED"}]
    carried = sql("SELECT carried_at IS NOT NULL, carried_reason FROM payment_obligations WHERE id = (SELECT "
                  "obligation_id FROM payment_instructions WHERE id = :i)", i=ivan_id)[0]
    assert carried[0] is True and "carried" in carried[1]
    assert "RUN_RECONCILED_WITH_CARRIED_ITEMS" in audit_actions(pay["cid"], "payroll_run", run_id)
    assert status_of(emma_id)[0] == "RECONCILED"


def test_camt053_statement_of_another_account_is_refused(pay):
    camt = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt><GrpHdr><MsgId>M1</MsgId></GrpHdr>
<Stmt><Id>S1</Id><Acct><Id><IBAN>AE070331234567890123456</IBAN></Id><Ccy>INR</Ccy></Acct>
<Ntry><Amt Ccy="INR">10.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>2026-10-30</Dt></BookgDt><ValDt><Dt>2026-10-31</Dt></ValDt></Ntry></Stmt></BkToCstmrStmt></Document>"""
    response = pay["fran"].post(IMPORT, data={"funding_account_id": str(pay["funding"]), "format": "CAMT053"},
                                files={"file": ("camt.xml", camt, "application/xml")})
    assert response.status_code == 409 and response.json()["code"] == "STATEMENT_ACCOUNT_MISMATCH", response.text
    assert sql("SELECT count(*) FROM bank_statement_imports")[0][0] == 0


def test_statement_import_validation_and_permissions(pay):
    err(pay["fran"].post(IMPORT, data={"funding_account_id": str(pay["funding"]), "format": "CSV"},
                         files={"file": ("s.csv", b"Value Date,Amount\r\n", "text/csv")}), 400, "MAPPING_REQUIRED")
    err(pay["fran"].post(IMPORT, data={"funding_account_id": str(pay["funding"]), "format": "CSV",
                                       "mapping": json.dumps({"date": "Value Date", "evil": "x"})},
                         files={"file": ("s.csv", b"x", "text/csv")}), 400, "MAPPING_INVALID")
    err(pay["fran"].post(IMPORT, data={"funding_account_id": "999999", "format": "CSV",
                                       "mapping": json.dumps(MAPPING)},
                         files={"file": ("s.csv", b"x", "text/csv")}), 404)
    processor = login_as(pay["app"], pay["admin"], create_employee(pay["admin"], "Pia"), ["payroll_processor"])
    err(processor.post(IMPORT, data={"funding_account_id": str(pay["funding"]), "format": "CSV",
                                     "mapping": json.dumps(MAPPING)}, files={"file": ("s.csv", b"x", "text/csv")}),
        403)
    err(processor.get(MATCHES), 403)
    err(processor.get(f"{PAY}/reconciliation/overdue"), 403)
    assert BATCHES  # imported for symmetry with the other modules
