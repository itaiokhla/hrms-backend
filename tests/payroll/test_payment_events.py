"""Payment evidence, events, failures, returns, reissue and manual proof (3.7 §7.3, §8.1, §10; 3.5 §5.9;
decision I-17; F-10; EC-PAY-02, EC-PAY-03, EC-PAY-04, EC-PAY-07, EC-PAY-09, EC-PAY-14)."""

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import (
    BATCHES,
    INSTRUCTIONS,
    MY_PAYMENTS,
    OBLIGATIONS,
    add_bank_account,
    end_bank_account,
    err,
    finalized_run,
    instructions_of,
    ok,
    pay,  # noqa: F401  (fixture)
    run_status,
    step_up,
    submitted_batch,
)
from tests.helpers import create_employee
from tests.payroll.test_wage_inputs import CHECK, RAISED, audit_actions, expect_error, session, sql, tenant

PDF = b"%PDF-1.4\n% proof of payment\n"


def evidence(cid, instruction_id, status, *, source="STATEMENT", event_id=None, amount=None, sandbox=False,
             reason_code=None):
    """Apply one piece of evidence through the service, as a statement, webhook or bank report would."""
    from app.payroll.services import payment_events as events

    with session(cid) as (db, _):
        row = events.load_instruction(db, cid, instruction_id)
        result = events.apply_evidence(db, None, row, status, source=source,
                                       evidence_source=None if source == "USER" else source,
                                       external_event_id=event_id, evidence_ref=event_id, sandbox=sandbox,
                                       amount=None if amount is None else Decimal(amount), reason_code=reason_code)
        db.commit()
        return result.outcome, result.reason


def instruction(instruction_id):
    row = sql("SELECT status, evidence_status, evidence_source, settled_amount, failure_code, failure_message, "
              "obligation_id FROM payment_instructions WHERE id = :i", i=instruction_id)[0]
    return dict(zip(("status", "evidence_status", "evidence_source", "settled_amount", "failure_code",
                     "failure_message", "obligation_id"), row))


def sandbox_world(pay, nets):
    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX' WHERE id = :f", f=pay["funding"])
    run_id = finalized_run(pay, nets)
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id, "method": "API"}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    step_up(pay["fran"])
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    return run_id, batch["id"]


def test_ec_pay_02_acknowledged_is_not_settled_and_ec_pay_07_never_moves_backwards(pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    assert evidence(pay["cid"], iid, "ACKNOWLEDGED", source="WEBHOOK", event_id="evt-ack-1", sandbox=True)[0] == "APPLIED"
    row = instruction(iid)
    assert (row["status"], row["evidence_status"]) == ("ACKNOWLEDGED", "UNVERIFIED")  # EC-PAY-02
    assert run_status(run_id) == "SUBMITTED"
    # Out of order: SETTLED arrives, then a late PROCESSING and ACKNOWLEDGED (EC-PAY-07).
    assert evidence(pay["cid"], iid, "SETTLED", source="WEBHOOK", event_id="evt-set-1", sandbox=True)[0] == "APPLIED"
    assert evidence(pay["cid"], iid, "PROCESSING", source="WEBHOOK", event_id="evt-proc-1", sandbox=True)[0] == "STALE"
    assert evidence(pay["cid"], iid, "ACKNOWLEDGED", source="WEBHOOK", event_id="evt-ack-2", sandbox=True)[0] == "STALE"
    row = instruction(iid)
    assert (row["status"], row["evidence_status"], row["evidence_source"]) == ("SETTLED", "VERIFIED", "WEBHOOK")
    assert run_status(run_id) == "PAID"
    history = ok(pay["fran"].get(f"{INSTRUCTIONS}/{iid}/events"))["events"]
    assert [(e["event_status"], e["outcome"]) for e in history][-4:] == [
        ("ACKNOWLEDGED", "APPLIED"), ("SETTLED", "APPLIED"), ("PROCESSING", "STALE"), ("ACKNOWLEDGED", "STALE")]
    # A duplicate provider event id is processed once.
    assert evidence(pay["cid"], iid, "SETTLED", source="WEBHOOK", event_id="evt-set-1", sandbox=True)[0] == "DUPLICATE"
    # A failure after settlement is a conflict for review; the status stays.
    assert evidence(pay["cid"], iid, "FAILED", source="WEBHOOK", event_id="evt-fail-1", sandbox=True) == (
        "CONFLICT", "FAILED_AFTER_SETTLEMENT")
    assert instruction(iid)["status"] == "SETTLED"


def test_ec_pay_03_partial_settlement_moves_the_run_to_partially_paid_then_paid(pay):
    run_id, batch_id = sandbox_world(pay, {"Emma": "100.00", "Ivan": "200.00"})
    rows = instructions_of(run_id)
    emma, ivan = rows[pay["emp"]["Emma"]["id"]]["id"], rows[pay["emp"]["Ivan"]["id"]]["id"]
    evidence(pay["cid"], emma, "SETTLED", source="WEBHOOK", event_id="evt-e-1", sandbox=True)
    assert run_status(run_id) == "PARTIALLY_PAID"
    assert sql("SELECT status FROM payment_batches WHERE id = :b", b=batch_id)[0][0] == "PARTIALLY_SETTLED"
    # Less than the instruction arrived: PARTIALLY_SETTLED, never SETTLED.
    evidence(pay["cid"], ivan, "SETTLED", source="WEBHOOK", event_id="evt-i-1", sandbox=True, amount="150.00")
    assert instruction(ivan)["status"] == "PARTIALLY_SETTLED"
    assert instruction(ivan)["settled_amount"] == Decimal("150.0000")
    assert run_status(run_id) == "PARTIALLY_PAID"
    evidence(pay["cid"], ivan, "SETTLED", source="WEBHOOK", event_id="evt-i-2", sandbox=True)
    assert run_status(run_id) == "PAID"
    assert sql("SELECT status FROM payment_batches WHERE id = :b", b=batch_id)[0][0] == "SETTLED"
    obligations = ok(pay["pete"].get(OBLIGATIONS, params={"run_id": run_id}))["items"]
    assert {o["status"] for o in obligations} == {"SETTLED"}
    assert {o["outstanding"]["amount"] for o in obligations} == {"0.00"}  # EC-PAY-23


def test_f10_database_refuses_settled_without_verified_evidence_and_events_are_append_only(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    with tenant(pay["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE payment_instructions SET status = 'SETTLED' WHERE id = :i", i=iid)
        expect_error(conn, CHECK, "UPDATE payment_instructions SET evidence_status = 'VERIFIED' WHERE id = :i", i=iid)
        event_id = conn.execute(text("SELECT id FROM payment_events WHERE instruction_id = :i LIMIT 1"),
                                {"i": iid}).scalar()
        for statement in ("UPDATE payment_events SET outcome = 'STALE' WHERE id = :e",
                          "DELETE FROM payment_events WHERE id = :e"):
            with pytest.raises(Exception) as info:
                with conn.begin_nested():
                    conn.execute(text(statement), {"e": event_id})
            assert getattr(info.value, "orig", None) is not None and info.value.orig.pgcode in (CHECK, "42501", RAISED)


def test_ec_pay_14_manual_proof_needs_an_upload_and_a_second_reviewer(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    url = f"{INSTRUCTIONS}/{iid}/manual-settle"
    form = {"method": "CHEQUE", "reference": "CHQ 000123"}
    err(pay["fran"].post(url, data=form, files={"file": ("proof.txt", b"just text", "text/plain")}), 400,
        "EVIDENCE_TYPE")
    proof = ok(pay["fran"].post(url, data=form, files={"file": ("proof.pdf", PDF, "application/pdf")}), 201)
    assert proof["status"] == "PENDING_REVIEW"
    assert instruction(iid)["status"] == "SUBMITTED"  # nothing settles on upload
    err(pay["fran"].post(url, data=form, files={"file": ("proof.pdf", PDF, "application/pdf")}), 409, "PROOF_PENDING")
    # The uploader can never review the proof (service and database).
    err(pay["fran"].post(f"/api/v1/payroll/manual-settlements/{proof['id']}/approve"), 403,
        "SECOND_REVIEWER_REQUIRED")
    with tenant(pay["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE payment_manual_settlements SET status = 'APPROVED', reviewed_at = now(), "
                     "reviewed_by_user_id = submitted_by_user_id WHERE id = :p", p=proof["id"])
    reviewer = ok(pay["pete"].post(f"/api/v1/payroll/manual-settlements/{proof['id']}/approve",
                                   json={"note": "Cheque cleared"}))
    assert reviewer["status"] == "APPROVED"
    row = instruction(iid)
    assert (row["status"], row["evidence_status"], row["evidence_source"]) == ("SETTLED", "VERIFIED", "MANUAL_PROOF")
    assert "MANUAL_SETTLEMENT" in audit_actions(pay["cid"], "payment_instruction", iid)
    assert run_status(run_id) == "PAID"
    emma = login_as(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])
    label = ok(emma.get(MY_PAYMENTS))["items"][0]["instructions"][0]["label"]["text"]
    assert label.startswith("Paid on ")
    # Without the reconcile permission nobody uploads proof.
    processor = login_as(pay["app"], pay["admin"], create_employee(pay["admin"], "Pia"), ["payroll_processor"])
    err(processor.post(url, data=form, files={"file": ("proof.pdf", PDF, "application/pdf")}), 403)


def test_rejected_manual_proof_needs_a_reason_and_settles_nothing(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    proof = ok(pay["fran"].post(f"{INSTRUCTIONS}/{iid}/manual-settle", data={"method": "CASH", "reference": "R-1"},
                                files={"file": ("proof.png", b"\x89PNG\r\n\x1a\n...", "image/png")}), 201)
    err(pay["pete"].post(f"/api/v1/payroll/manual-settlements/{proof['id']}/reject", json={"note": ""}), 422)
    rejected = ok(pay["pete"].post(f"/api/v1/payroll/manual-settlements/{proof['id']}/reject",
                                   json={"note": "Receipt is illegible"}))
    assert rejected["status"] == "REJECTED" and instruction(iid)["status"] == "SUBMITTED"


def test_ec_pay_04_return_reopens_the_obligation_fails_the_account_and_reissue_needs_approval(pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00", "Ivan": "200.00"})
    rows = instructions_of(run_id)
    emma, ivan = rows[pay["emp"]["Emma"]["id"]]["id"], rows[pay["emp"]["Ivan"]["id"]]["id"]
    evidence(pay["cid"], emma, "SETTLED", source="WEBHOOK", event_id="evt-r-1", sandbox=True)
    evidence(pay["cid"], ivan, "SETTLED", source="WEBHOOK", event_id="evt-r-2", sandbox=True)
    assert run_status(run_id) == "PAID"
    assert evidence(pay["cid"], emma, "RETURNED", source="WEBHOOK", event_id="evt-r-3", sandbox=True,
                    reason_code="AC04")[0] == "APPLIED"
    row = instruction(emma)
    assert row["status"] == "RETURNED" and row["failure_code"] == "AC04" and row["failure_message"]
    assert sql("SELECT status FROM payment_obligations WHERE id = :o", o=row["obligation_id"])[0][0] == "OPEN"
    assert run_status(run_id) == "PARTIALLY_PAID"
    account = sql("SELECT id, verification_status FROM bank_accounts WHERE employee_id = :e",
                  e=pay["emp"]["Emma"]["id"])[0]
    assert account[1] == "FAILED"  # EC-BNK-10
    assert "PAYMENT_FAILED" in audit_actions(pay["cid"], "payment_instruction", emma)
    emma_client = login_as(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])
    mine = ok(emma_client.get(MY_PAYMENTS))["items"][0]
    assert mine["instructions"][0]["label"]["text"] == "Payment failed — please check your bank details"
    assert mine["outstanding"]["amount"] == "100.00"

    # No usable account yet: fix the bank details first.
    err(pay["pete"].post(f"{INSTRUCTIONS}/{emma}/reissue", json={"reason": "Account closed"}), 409,
        "BANK_ACCOUNT_NOT_USABLE")
    new_account = add_bank_account(pay["cid"], pay["emp"]["Emma"]["id"], "501008888888", holder="Emma Test",
                                   replaces=account[0])
    end_bank_account(account[0], replaced_by=new_account)
    requested = ok(pay["pete"].post(f"{INSTRUCTIONS}/{emma}/reissue", json={"reason": "Account closed"}))
    assert requested["status"] == "APPROVAL_PENDING" and requested["created"] == []
    err(pay["pete"].post(f"{INSTRUCTIONS}/{emma}/reissue/approve"), 403, "DUAL_CONTROL")
    approved = ok(pay["fran"].post(f"{INSTRUCTIONS}/{emma}/reissue/approve", json={"note": "New account verified"}))
    (created,) = approved["created"]
    assert approved["status"] == "REISSUED" and instruction(emma)["status"] == "REISSUED"
    new_row = sql("SELECT status, amount, obligation_id, reissue_of_instruction_id, bank_account_id FROM "
                  "payment_instructions WHERE id = :i", i=created["id"])[0]
    assert tuple(new_row) == ("READY", Decimal("100.0000"), row["obligation_id"], emma, new_account)
    # No new earnings: the same single obligation.
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=run_id)[0][0] == 2
    assert "PAYMENT_REISSUED" in audit_actions(pay["cid"], "payment_instruction", emma)
    err(pay["fran"].post(f"{INSTRUCTIONS}/{emma}/reissue", json={"reason": "Again"}), 409, "INVALID_TRANSITION")


def test_reissue_without_required_approval_is_immediate(pay):
    from tests.payroll.test_wage_inputs import set_setting

    set_setting(pay["cid"], "payroll.payments.reissue_requires_approval", False)
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    evidence(pay["cid"], emma, "FAILED", source="WEBHOOK", event_id="evt-f-1", sandbox=True)
    old = sql("SELECT id FROM bank_accounts WHERE employee_id = :e", e=pay["emp"]["Emma"]["id"])[0][0]
    new = add_bank_account(pay["cid"], pay["emp"]["Emma"]["id"], "501008888889", holder="Emma Test", replaces=old)
    end_bank_account(old, replaced_by=new)
    result = ok(pay["pete"].post(f"{INSTRUCTIONS}/{emma}/reissue", json={"reason": "Fixed account"}))
    assert result["status"] == "REISSUED" and len(result["created"]) == 1


def test_ec_pay_09_insufficient_funds_rejection_fails_the_payment_with_a_plain_message(pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    assert evidence(pay["cid"], emma, "FAILED", source="WEBHOOK", event_id="evt-nsf", sandbox=True,
                    reason_code="AM04")[0] == "APPLIED"
    row = instruction(emma)
    assert row["status"] == "FAILED" and row["failure_message"] == "Insufficient funds in the funding account."
    assert "PAYMENT_FAILED" in audit_actions(pay["cid"], "payment_instruction", emma)
    # A settlement after the failure is a conflict, never a silent "paid".
    assert evidence(pay["cid"], emma, "SETTLED", source="WEBHOOK", event_id="evt-late", sandbox=True) == (
        "CONFLICT", "SETTLED_AFTER_FAILURE")
    assert instruction(emma)["status"] == "FAILED"


def test_evidence_for_an_unsent_payment_is_a_conflict(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    assert evidence(pay["cid"], emma, "SETTLED", event_id="line:x") == ("CONFLICT", "NOT_SUBMITTED")
    assert instruction(emma)["status"] == "READY"


def test_ec_pay_21_failure_notifications_audit_and_history_never_carry_full_bank_details(pay):
    import json
    import os
    from pathlib import Path

    from tests.payroll.test_payment_obligations import NUMBERS

    login_as(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])  # Emma has a user to notify
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    assert evidence(pay["cid"], emma, "RETURNED", source="WEBHOOK", event_id="evt-ret-21", sandbox=True,
                    reason_code="AC04")[0] == "APPLIED"
    number = NUMBERS["Emma"]
    texts = [json.dumps([str(v) for v in r]) for r in sql(
        "SELECT title, message FROM notifications WHERE company_id = :c", c=pay["cid"])]
    assert texts, "the employee is told about the failed payment"
    texts += [json.dumps(r[0]) for r in sql("SELECT details FROM audit_logs WHERE company_id = :c", c=pay["cid"])]
    texts += [json.dumps(r[0]) for r in sql("SELECT details FROM payment_events WHERE company_id = :c", c=pay["cid"])]
    texts += [json.dumps(r[0]) for r in sql("SELECT payload FROM jobs WHERE company_id = :c", c=pay["cid"])]
    outbox_dir = Path(os.environ["EMAIL_OUTBOX_DIR"]) / str(pay["cid"])
    if outbox_dir.exists():
        texts += [p.read_text(encoding="utf-8", errors="replace") for p in outbox_dir.glob("*") if p.is_file()]
    assert not [t for t in texts if number in t]
    # The masked account (last digits only) is what the employee and finance see.
    detail = ok(pay["fran"].get(f"{INSTRUCTIONS}/{emma}"))
    assert detail["beneficiary"]["account_masked"].endswith(number[-4:]) and number not in json.dumps(detail)
