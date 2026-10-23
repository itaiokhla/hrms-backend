"""Independent verification of the phase-5 follow-up fixes (test_phase5_followups.py): adversarial paths the
fixer's tests did not take.

1. EC-COR-03/04: in-flight payments of a reversed employee that settle through the OTHER settlement paths
   (manual proof with a second reviewer, a SANDBOX provider webhook) become exactly one recovery, also when
   the webhook is replayed; a payment of an employee the partial reversal did not cover, and a payment of a
   run whose reversal was rejected, create no recovery.
2. EC-DOC-11: a failing publication re-check never undoes the settlement it follows (savepoint), and an
   accepted SANDBOX API release publishes ON_PAYMENT_SUBMITTED payslips at once.
3. F-11: two OVERLAPPING statements (different files sharing a line) imported at the same moment store the
   shared line once and never fail on uq_bank_statement_lines_hash (two real sessions in two threads).
4. SoD (3.1: preparer ≠ approver ≠ payment releaser): with payroll.approval.separation_of_duties ON and
   payment dual control OFF, the batch preparer still cannot release the batch.
5. EC-BNK-07: a refused (no fresh step-up) attempt to lift a payment hold leaves no trace of a review, and
   another user's step-up does not count.
"""

import threading
from datetime import date

from tests.helpers import create_employee
from tests.payroll.test_bank_accounts import (  # noqa: F401  (bank_world, world are fixtures)
    STAFF,
    account_row,
    active_account,
    bank_world,
    in_account,
    payee_url,
    verify_with_document,
    world,
)
from tests.payroll.test_bank_accounts import step_up as bank_step_up
from tests.payroll.test_bank_accounts import user_id as bank_user_id
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import (  # noqa: F401  (pay is a fixture)
    BATCHES,
    err,
    instructions_of,
    ok,
    pay,
    run_status,
    step_up,
    submitted_batch,
)
from tests.payroll.test_payment_obligations import finalized_run as paid_run
from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    add_instruction,
    docs_world,
    emma_run,
    generate,
    payslip_of,
)
from tests.payroll.test_phase5_followups import (
    evidence,
    finalized_reversal,
    instructions_of_row,
    mark_submitted,
    reversal_of,
    reversal_recoveries,
)
from tests.payroll.test_reconciliation import MAPPING, csv_statement
from tests.payroll.test_retro import finalize, retro_world, run_employee_id, runs_world  # noqa: F401
from tests.payroll.test_reversals import REVERSALS, reversal_world, reverse  # noqa: F401
from tests.payroll.test_wage_inputs import audit_actions, session, set_setting, sql

SOD = "payroll.approval.separation_of_duties"
PDF = b"%PDF-1.4\n% proof of a cheque payment\n%%EOF\n"


# ---------------------------------------------------------------------------
# 1. EC-COR-03/04 through manual proof and the SANDBOX webhook
# ---------------------------------------------------------------------------
def _reconcilers(app, w):
    return [login_as(app, w["acme"], create_employee(w["acme"], first, "Recon"), ["finance_payments"])
            for first in ("Rita", "Rolf")]


def _uid(client):
    return sql("SELECT id FROM users WHERE email = :e", e=client.email)[0][0]


def _manual_settle(cid, instruction_id, uploader, reviewer):
    """Proof uploaded by one reconciler, accepted by a second (3.7 §7.3, EC-PAY-14), committed."""
    from app.payroll.services import payment_events

    with session(cid, _uid(uploader)) as (db, principal):
        proof = payment_events.submit_manual_proof(db, principal, instruction_id, method="CHEQUE",
                                                   reference="CHQ-000123", note=None, file_name="proof.pdf",
                                                   content=PDF)
        db.commit()
        proof_id = proof.id
    with session(cid, _uid(reviewer)) as (db, principal):
        payment_events.review_manual_proof(db, principal, proof_id, approve=True, note=None)
        db.commit()


def _webhook(cid, instruction_id, event_id):
    """A verified SANDBOX provider event applied in the tenant session, as the webhook route does."""
    from app.payroll.services import payment_events

    with session(cid) as (db, _):
        row = payment_events.load_instruction(db, cid, instruction_id)
        result = payment_events.apply_evidence(db, None, row, "SETTLED", source="WEBHOOK", evidence_source="WEBHOOK",
                                               external_event_id=event_id, evidence_ref=f"SANDBOX:{event_id}",
                                               sandbox=True)
        db.commit()
        return result.outcome


def test_ec_cor_03_verify_manual_proof_and_sandbox_webhook_settlements_after_the_reversal(app, reversal_world):
    w = reversal_world
    cid, run_id, emma_row = w["cid"], w["run"]["id"], w["emma_row"]
    ivan_row = run_employee_id(run_id, w["ivan"])
    emma_payments, ivan_payments = instructions_of_row(emma_row), instructions_of_row(ivan_row)
    assert emma_payments and ivan_payments
    for instruction_id, _s, _a in emma_payments + ivan_payments:
        mark_submitted(instruction_id)
    # Ivan's payments went through the SANDBOX payout provider (a SANDBOX payment always carries the
    # provider's reference: ck_payment_instructions_sandbox_ref).
    for instruction_id, _s, _a in ivan_payments:
        sql("UPDATE payment_instructions SET submission_channel = 'SANDBOX', provider_ref = :r WHERE id = :i",
            r=f"SBX-REF-{instruction_id}", i=instruction_id)
    reversal_id, reversal_run = finalized_reversal(w, [emma_row, ivan_row])
    finalize(w, reversal_run)
    assert reversal_of(w, reversal_id)["status"] == "FINALIZED"
    assert reversal_recoveries(emma_row) == reversal_recoveries(ivan_row) == []
    rita, rolf = _reconcilers(app, w)

    # Manual proof of Emma's bank-file payments, each reviewed by a second person.
    for instruction_id, _s, _a in emma_payments:
        _manual_settle(cid, instruction_id, rita, rolf)
        assert sql("SELECT status FROM payment_instructions WHERE id = :i", i=instruction_id)[0][0] == "SETTLED"
    recoveries = reversal_recoveries(emma_row)
    assert sorted((ref["instruction_id"], settled) for _p, _st, ref, settled, _d in recoveries) == sorted(
        (i, a) for i, _s, a in emma_payments)
    # SANDBOX webhook settlements of Ivan's payments, then each event replayed and re-sent under a new id.
    for n, (instruction_id, _s, _a) in enumerate(ivan_payments):
        assert _webhook(cid, instruction_id, f"sbx-{n}") == "APPLIED"
        assert _webhook(cid, instruction_id, f"sbx-{n}") == "DUPLICATE"
        assert _webhook(cid, instruction_id, f"sbx-again-{n}") != "APPLIED"
    ivan_recoveries = reversal_recoveries(ivan_row)
    assert len(ivan_recoveries) == len(ivan_payments)
    assert sum(settled for *_x, settled, _d in ivan_recoveries) == sum(a for _i, _s, a in ivan_payments)
    assert all(status == "APPROVED" and delta == -settled
               for _p, status, _r, settled, delta in recoveries + ivan_recoveries)
    effects = reversal_of(w, reversal_id)["effects"]
    assert len(effects["recoveries"]) == len(emma_payments) + len(ivan_payments)
    assert all(r["settled_after_reversal"] for r in effects["recoveries"])
    # Never a bank debit.
    assert sql("SELECT count(*) FROM payment_instructions WHERE run_id = :r", r=reversal_run)[0][0] == 0


def test_ec_cor_03_verify_an_employee_outside_a_partial_reversal_gets_no_recovery(reversal_world):
    w = reversal_world
    cid, run_id, emma_row = w["cid"], w["run"]["id"], w["emma_row"]
    ivan_row = run_employee_id(run_id, w["ivan"])
    ivan_payments = instructions_of_row(ivan_row)
    for instruction_id, _s, _a in instructions_of_row(emma_row) + ivan_payments:
        mark_submitted(instruction_id)
    reversal_id, reversal_run = finalized_reversal(w, [emma_row])
    finalize(w, reversal_run)
    for n, (instruction_id, _s, _a) in enumerate(ivan_payments):
        assert evidence(cid, instruction_id, event_id=f"ivan-late-{n}") == "APPLIED"
    assert reversal_recoveries(ivan_row) == []
    assert reversal_of(w, reversal_id)["effects"]["recoveries"] == []


def test_ec_cor_03_verify_a_rejected_reversal_never_turns_a_settlement_into_a_recovery(reversal_world):
    w = reversal_world
    cid, emma_row = w["cid"], w["emma_row"]
    payments = instructions_of_row(emma_row)
    for instruction_id, _s, _a in payments:
        mark_submitted(instruction_id)
    created = reverse(w["acme"], w["run"]["id"], run_employee_ids=[emma_row])
    r = w["approver"].post(f"{REVERSALS}/{created['id']}/reject", json={"reason": "Not wrong after all"})
    assert r.status_code == 200, r.text
    for n, (instruction_id, _s, _a) in enumerate(payments):
        assert evidence(cid, instruction_id, event_id=f"after-reject-{n}") == "APPLIED"
    assert reversal_recoveries(emma_row) == []
    assert "REVERSAL_RECOVERY_ADDED" not in audit_actions(cid, "payroll_reversal", created["id"])


# ---------------------------------------------------------------------------
# 2. EC-DOC-11: publication is best effort and covers the SANDBOX API release
# ---------------------------------------------------------------------------
def test_ec_doc_11_verify_a_failing_publication_recheck_never_undoes_the_settlement(docs_world, monkeypatch):
    from sqlalchemy import text

    from app.payroll.services import publication

    w = docs_world
    cid = w["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_SETTLED")
    emma_id = w["emp"]["Emma"]["id"]
    run_id, rows = emma_run(w)
    emma_pay = add_instruction(cid, run_id, rows[emma_id], emma_id, w["le01"], w["group"], status="SUBMITTED")
    generate(cid, run_id)
    assert payslip_of(rows[emma_id])[1] == "GENERATED"
    real = publication.on_payment_progress
    calls = []

    def broken(db, company_id, run):
        calls.append(run)
        db.execute(text("SELECT 1 / 0"))  # aborts the savepoint, as a database error inside publication would

    monkeypatch.setattr(publication, "on_payment_progress", broken)
    assert evidence(cid, emma_pay, event_id="stmt-broken") == "APPLIED"
    assert calls == [run_id]
    # The settlement committed although publication failed; the payslip waits for the sweep.
    assert sql("SELECT status, evidence_status FROM payment_instructions WHERE id = :i", i=emma_pay)[0] == (
        "SETTLED", "VERIFIED")
    assert payslip_of(rows[emma_id])[1] == "GENERATED"
    monkeypatch.setattr(publication, "on_payment_progress", real)
    with session(cid) as (db, _):
        publication.apply_publication(db, cid, run_id)  # the hourly sweep
        db.commit()
    assert payslip_of(rows[emma_id])[1] == "PUBLISHED"


def test_ec_doc_11_verify_sandbox_api_release_publishes_on_payment_submitted_payslips(pay):
    cid = pay["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_PAYMENT_SUBMITTED")
    set_setting(cid, "payslip.email_notify", False)
    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX' WHERE id = :f", f=pay["funding"])
    run_id = paid_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    generate(cid, run_id)
    rows = [r for (r,) in sql("SELECT id FROM payroll_run_employees WHERE run_id = :r", r=run_id)
            if payslip_of(r) is not None]
    assert len(rows) == 2 and {payslip_of(r)[1] for r in rows} == {"GENERATED"}
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id, "method": "API"}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    assert {payslip_of(r)[1] for r in rows} == {"GENERATED"}  # approval is not submission
    step_up(pay["fran"])
    assert ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))["status"] == "SUBMITTED"
    assert {payslip_of(r)[1] for r in rows} == {"PUBLISHED"}


# ---------------------------------------------------------------------------
# 3. F-11: overlapping statements imported concurrently
# ---------------------------------------------------------------------------
def test_f11_verify_concurrent_overlapping_statements_store_the_shared_line_once(pay):
    """Two real sessions in two threads. A imports a statement with Emma's line and holds its transaction;
    B imports a DIFFERENT file with Emma's and Ivan's lines (so the file_sha256 check does not help). Without
    the funding-account lock B would not see A's uncommitted line and fail on uq_bank_statement_lines_hash."""
    from app.payroll.services import reconciliation

    cid, fran = pay["cid"], pay["fran"]
    run_id = paid_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    submitted_batch(pay, run_id)
    refs = {e: ref for e, ref in sql("SELECT employee_id, reference FROM payment_instructions WHERE run_id = :r",
                                     r=run_id)}
    today = date.today().isoformat()
    emma_line = (today, "-100.00", refs[pay["emp"]["Emma"]["id"]], "")
    ivan_line = (today, "-200.00", refs[pay["emp"]["Ivan"]["id"]], "")
    first_file, second_file = csv_statement(emma_line), csv_statement(emma_line, ivan_line)
    outcome = {}

    def second_import():
        try:
            with session(cid, fran.user_id) as (db, principal):
                outcome["result"] = reconciliation.import_statement(
                    db, principal, funding_account_id=pay["funding"], fmt="CSV", file_name="week.csv",
                    content=second_file, mapping=MAPPING)
                db.commit()
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            outcome["error"] = exc

    with session(cid, fran.user_id) as (db, principal):
        first = reconciliation.import_statement(db, principal, funding_account_id=pay["funding"], fmt="CSV",
                                                file_name="day.csv", content=first_file, mapping=MAPPING)
        assert first["duplicate"] is False
        thread = threading.Thread(target=second_import, daemon=True)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive()
        db.commit()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    second = outcome["result"]
    assert second["duplicate"] is False and second["import"]["id"] != first["import"]["id"]
    assert (second["import"]["line_count"], second["import"]["new_line_count"]) == (2, 1)
    assert sql("SELECT count(*) FROM bank_statement_imports WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 2
    assert sql("SELECT count(*) FROM bank_statement_lines WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 2
    assert {v["status"] for v in instructions_of(run_id).values()} <= {"SETTLED", "RECONCILED"}


# ---------------------------------------------------------------------------
# 4. Separation of duties without payment dual control
# ---------------------------------------------------------------------------
def test_sod_verify_preparer_cannot_release_even_with_payment_dual_control_off(pay):
    """3.1: "Preparer, approver and payment releaser must be different people" while SoD is ON. With
    payroll.approval.payment_dual_control OFF the DUAL_CONTROL guard no longer stops the preparer, so the SoD
    check has to (before the fix Pete prepared and released the batch himself)."""
    set_setting(pay["cid"], "payroll.approval.payment_dual_control", False)
    sql("UPDATE funding_accounts SET signatory_user_ids = CAST(:s AS integer[]) WHERE id = :f",
        s="{" + ",".join(str(pay[n].user_id) for n in ("fran", "pete")) + "}", f=pay["funding"])
    run_id = paid_run(pay, {"Emma": "100.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    step_up(pay["pete"])
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/release"), 403, "SEPARATION_OF_DUTIES")
    assert sql("SELECT status, released_by_user_id FROM payment_batches WHERE id = :b", b=batch["id"])[0] == (
        "FILE_GENERATED", None)
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}
    # With SoD OFF as well, nothing separates the duties any more: the preparer may release.
    set_setting(pay["cid"], SOD, False)
    assert ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/release"))["status"] == "SUBMITTED"


def test_sod_verify_preparer_who_also_approved_cannot_release_with_dual_control_off(pay):
    set_setting(pay["cid"], "payroll.approval.payment_dual_control", False)
    sql("UPDATE funding_accounts SET signatory_user_ids = CAST(:s AS integer[]) WHERE id = :f",
        s="{" + str(pay["fran"].user_id) + "}", f=pay["funding"])
    run_id = paid_run(pay, {"Emma": "100.00"})
    batch = ok(pay["fran"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))  # dual control OFF: the preparer may approve
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    step_up(pay["fran"])
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 403, "SEPARATION_OF_DUTIES")


# ---------------------------------------------------------------------------
# 5. EC-BNK-07: a refused hold release leaves no trace; step-up is per user
# ---------------------------------------------------------------------------
def _held_duplicate(w):
    emma_account, _ = active_account(w, w["u"]["emma"], in_account())
    verify_with_document(w, emma_account, w["u"]["emma"])
    ivan_account, _ = active_account(w, w["u"]["pia"], in_account("Ivan Test"), url=payee_url(w["emp"]["Ivan"]))
    held = account_row(ivan_account)
    assert (held["payment_hold"], held["hold_reason"]) == (True, "DUPLICATE_FINGERPRINT")
    return ivan_account


def _manual_reviews(account_id):
    return sql("SELECT count(*) FROM bank_verifications WHERE bank_account_id = :a AND method = 'MANUAL_REVIEW'",
               a=account_id)[0][0]


def test_ec_bnk_07_verify_refused_hold_release_leaves_no_review_and_step_up_is_per_user(bank_world):
    w = bank_world
    ava, hal = w["u"]["ava"], w["u"]["hal"]
    ivan_account = _held_duplicate(w)
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=bank_user_id(ava))
    bank_step_up(hal)  # someone else's fresh step-up does not count for Ava
    before = (account_row(ivan_account)["row_version"], _manual_reviews(ivan_account),
              audit_actions(w["cid"], "bank_account", ivan_account).count("BANK_REVIEW_CLEARED"))
    r = ava.post(f"{STAFF}/{ivan_account}/review", json={"action": "CLEAR_REVIEW", "reason": "Joint account, ok"})
    assert (r.status_code, r.json().get("code")) == (403, "STEP_UP_REQUIRED"), r.text
    row = account_row(ivan_account)
    assert (row["payment_hold"], row["hold_reason"]) == (True, "DUPLICATE_FINGERPRINT")
    after = (row["row_version"], _manual_reviews(ivan_account),
             audit_actions(w["cid"], "bank_account", ivan_account).count("BANK_REVIEW_CLEARED"))
    assert after == before  # no review recorded, no audit, no version bump
    bank_step_up(ava)
    r = ava.post(f"{STAFF}/{ivan_account}/review", json={"action": "CLEAR_REVIEW", "reason": "Joint account, ok"})
    assert r.status_code == 200 and r.json()["payment_hold"] is False, r.text
    assert _manual_reviews(ivan_account) == before[1] + 1
    assert audit_actions(w["cid"], "bank_account", ivan_account).count("BANK_REVIEW_CLEARED") == before[2] + 1
