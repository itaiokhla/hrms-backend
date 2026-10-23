"""Phase-5 follow-ups (open findings of the two phase-5 adversarial reviews, FOLLOW-UPS F-52 and the financial
review): each test reproduces the defect and pins the fix.

1. EC-COR-03 / EC-COR-04: a payment still in flight when its run employee's reversal is finalized, which settles
   later, becomes a recovery receivable (exactly what was settled, once, also when the evidence is replayed);
   never a bank debit.
2. EC-DOC-11: the payment services re-check payslip publication themselves (ON_PAYMENT_SUBMITTED after a
   release, ON_SETTLED after a settlement) instead of waiting for the hourly sweep.
3. F-11 / EC-PAY-06: two imports of the same statement at the same moment give one import and a duplicate,
   never a 500 (tested with two real sessions in two threads, plus a direct simulation of the race window
   for the savepoint fallback).
4. 3.1 §5.14 separation of duties (preparer ≠ approver ≠ payment releaser): the approver of a batch cannot
   release it while payroll.approval.separation_of_duties is ON.
5. EC-BNK-07 risk holds: lifting an account's payment hold (CLEAR_REVIEW / MARK_VERIFIED) needs a fresh step-up.
"""

import threading
from decimal import Decimal

import pytest

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
from tests.payroll.test_reconciliation import MAPPING, csv_statement
from tests.payroll.test_retro import finalize, retro_world, run_employee_id, runs_world  # noqa: F401
from tests.payroll.test_reversals import REVERSALS, approve, reversal_world, reverse  # noqa: F401
from tests.payroll.test_wage_inputs import audit_actions, session, set_setting, sql

SOD = "payroll.approval.separation_of_duties"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def instructions_of_row(run_employee):
    return [(i, s, Decimal(a)) for i, s, a in sql(
        "SELECT i.id, i.status, i.amount FROM payment_instructions i JOIN payment_obligations o ON "
        "o.id = i.obligation_id WHERE o.run_employee_id = :r ORDER BY i.id", r=run_employee)]


def mark_submitted(instruction_id):
    """The payment left with a bank file (in flight: SUBMITTED, no longer cancellable)."""
    sql("UPDATE payment_instructions SET status = 'SUBMITTED', submission_channel = 'MANUAL_UPLOAD', "
        "submitted_at = now(), row_version = row_version + 1 WHERE id = :i", i=instruction_id)


def evidence(cid, instruction_id, status="SETTLED", *, event_id, amount=None):
    """Bank statement evidence for one payment, applied and committed as the statement import does."""
    from app.payroll.services import payment_events

    with session(cid) as (db, _):
        row = payment_events.load_instruction(db, cid, instruction_id)
        result = payment_events.apply_evidence(db, None, row, status, source="STATEMENT", evidence_source="STATEMENT",
                                               external_event_id=event_id, amount=amount)
        db.commit()
        return result.outcome


def reversal_recoveries(run_employee):
    return [(p, status, ref, Decimal(settled), Decimal(delta)) for p, status, ref, settled, delta in sql(
        "SELECT p.id, p.status, p.trigger_ref, l.recalculated_amount, l.delta FROM arrears_proposals p "
        "JOIN arrears_proposal_lines l ON l.proposal_id = p.id WHERE p.original_run_employee_id = :r "
        "AND p.trigger = 'REVERSAL' ORDER BY p.id", r=run_employee)]


def reversal_of(w, reversal_id):
    return ok(w["acme"].get(f"{REVERSALS}/{reversal_id}"))


def finalized_reversal(w, run_employee_ids):
    created = reverse(w["acme"], w["run"]["id"], run_employee_ids=run_employee_ids)
    approve(w["approver"], created["id"])
    reversal_run = approve(w["second"], created["id"])["reversal_run_id"]
    return created["id"], reversal_run


# ---------------------------------------------------------------------------
# 1. EC-COR-03 / EC-COR-04: in-flight payment of a reversed employee that settles later
# ---------------------------------------------------------------------------
def test_ec_cor_03_ec_cor_04_in_flight_payment_settling_after_the_reversal_becomes_one_recovery(reversal_world):
    from app.payroll.services import reversals

    w = reversal_world
    cid, run_id, emma_row = w["cid"], w["run"]["id"], w["emma_row"]
    payments = instructions_of_row(emma_row)
    assert payments
    for instruction_id, _status, _amount in payments:
        mark_submitted(instruction_id)
    paid = sum(amount for _i, _s, amount in payments)
    reversal_id, reversal_run = finalized_reversal(w, [emma_row])
    finalize(w, reversal_run)
    data = reversal_of(w, reversal_id)
    # Finalization could not cancel the payment (already at the bank) and had nothing settled to recover.
    assert data["status"] == "FINALIZED" and data["effects"]["recoveries"] == []
    assert sorted(data["effects"]["instructions_in_flight"]) == [i for i, _s, _a in payments]
    assert reversal_recoveries(emma_row) == []

    # The bank pays it anyway: the settlement itself records the recovery receivable.
    for n, (instruction_id, _status, _amount) in enumerate(payments):
        assert evidence(cid, instruction_id, event_id=f"stmt-late-{n}") == "APPLIED"
    recoveries = reversal_recoveries(emma_row)
    assert len(recoveries) == len(payments)
    assert sum(settled for _p, _s, _r, settled, _d in recoveries) == paid
    assert all(status == "APPROVED" and delta == -settled and ref["id"] == reversal_id
               and ref["instruction_id"] in {i for i, _s, _a in payments}
               for _p, status, ref, settled, delta in recoveries)
    data = reversal_of(w, reversal_id)
    assert [(r["proposal_id"], r["settled_after_reversal"]) for r in data["effects"]["recoveries"]] == [
        (p, True) for p, *_ in recoveries]
    assert "REVERSAL_RECOVERY_ADDED" in audit_actions(cid, "payroll_reversal", reversal_id)

    # Idempotent: the same event replayed, the same settlement from another evidence source, and the hook
    # called again add nothing.
    for n, (instruction_id, _status, _amount) in enumerate(payments):
        assert evidence(cid, instruction_id, event_id=f"stmt-late-{n}") == "DUPLICATE"
        assert evidence(cid, instruction_id, event_id=f"stmt-again-{n}") != "APPLIED"
        with session(cid) as (db, _):
            from app.payroll.services import payment_events

            assert reversals.on_instruction_settled(db, payment_events.load_instruction(db, cid, instruction_id)) == []
            db.commit()
    assert reversal_recoveries(emma_row) == recoveries
    # Never a bank debit: the settled payment stays settled and no payment is created to take money back.
    assert {s for _i, s, _a in instructions_of_row(emma_row)} == {"SETTLED"}
    assert sql("SELECT count(*) FROM payment_instructions WHERE run_id = :r", r=reversal_run)[0][0] == 0


def test_ec_cor_03_partial_then_full_settlement_and_settlement_before_finalization_are_recovered_exactly_once(
        reversal_world):
    w = reversal_world
    cid, run_id, emma_row = w["cid"], w["run"]["id"], w["emma_row"]
    ivan_row = run_employee_id(run_id, w["ivan"])
    emma_payments, ivan_payments = instructions_of_row(emma_row), instructions_of_row(ivan_row)
    for instruction_id, _s, _a in emma_payments + ivan_payments:
        mark_submitted(instruction_id)
    reversal_id, reversal_run = finalized_reversal(w, [emma_row, ivan_row])
    # Ivan's payment settles while the reversal is APPROVED but not finalized: finalization counts it.
    for n, (instruction_id, _s, _a) in enumerate(ivan_payments):
        assert evidence(cid, instruction_id, event_id=f"ivan-{n}") == "APPLIED"
    assert reversal_recoveries(ivan_row) == []
    finalize(w, reversal_run)
    ivan_paid = sum(a for _i, _s, a in ivan_payments)
    [(_p, _st, ivan_ref, ivan_settled, _d)] = reversal_recoveries(ivan_row)
    assert ivan_settled == ivan_paid and "instruction_id" not in ivan_ref  # from on_run_finalized, once

    # Emma's payment arrives in two parts after the reversal was finalized: the recoveries add up to what
    # settled, never more.
    instruction_id, _s, amount = emma_payments[0]
    half = (amount / 2).quantize(Decimal("0.01"))
    assert evidence(cid, instruction_id, event_id="emma-part", amount=half) == "APPLIED"
    assert sql("SELECT status FROM payment_instructions WHERE id = :i", i=instruction_id)[0][0] == "PARTIALLY_SETTLED"
    assert [settled for *_x, settled, _d in reversal_recoveries(emma_row)] == [half]
    assert evidence(cid, instruction_id, event_id="emma-rest", amount=amount) == "APPLIED"
    assert [settled for *_x, settled, _d in reversal_recoveries(emma_row)] == [half, amount - half]
    assert evidence(cid, instruction_id, event_id="emma-rest") == "DUPLICATE"
    assert sum(settled for *_x, settled, _d in reversal_recoveries(emma_row)) == amount
    # Ivan's later evidence (already settled and recovered at finalization) adds nothing.
    for n, (ivan_instruction, _s, _a) in enumerate(ivan_payments):
        evidence(cid, ivan_instruction, event_id=f"ivan-again-{n}")
    assert len(reversal_recoveries(ivan_row)) == 1
    effects = reversal_of(w, reversal_id)["effects"]
    assert len(effects["recoveries"]) == 3


# ---------------------------------------------------------------------------
# 2. EC-DOC-11: payment progress publishes payslips at once
# ---------------------------------------------------------------------------
def test_ec_doc_11_settlement_publishes_on_settled_payslips_without_the_sweep(docs_world):
    w = docs_world
    cid = w["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_SETTLED")
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    run_id, rows = emma_run(w)
    emma_pay = add_instruction(cid, run_id, rows[emma_id], emma_id, w["le01"], w["group"], status="SUBMITTED")
    add_instruction(cid, run_id, rows[ivan_id], ivan_id, w["le01"], w["group"], status="SUBMITTED")
    generate(cid, run_id)
    assert payslip_of(rows[emma_id])[1] == payslip_of(rows[ivan_id])[1] == "GENERATED"
    # Emma's payment settles on bank evidence: her payslip is published by that same transaction.
    assert evidence(cid, emma_pay, event_id="stmt-emma") == "APPLIED"
    assert payslip_of(rows[emma_id])[1] == "PUBLISHED" and payslip_of(rows[ivan_id])[1] == "GENERATED"
    doc = payslip_of(rows[emma_id])[0]
    notified = sql("SELECT count(*) FROM document_notifications WHERE document_id = :d", d=doc)[0][0]
    assert notified >= 1
    # Replayed evidence publishes nothing twice and notifies nobody again.
    assert evidence(cid, emma_pay, event_id="stmt-emma") == "DUPLICATE"
    assert sql("SELECT count(*) FROM document_notifications WHERE document_id = :d", d=doc)[0][0] == notified
    assert payslip_of(rows[ivan_id])[1] == "GENERATED"


def test_ec_doc_11_release_publishes_on_payment_submitted_payslips_without_the_sweep(pay):
    cid = pay["cid"]
    set_setting(cid, "payslip.publish_trigger", "ON_PAYMENT_SUBMITTED")
    set_setting(cid, "payslip.email_notify", False)
    run_id = paid_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    generate(cid, run_id)  # the finalized-run handler (idempotent)
    rows = {e: r for r, e in sql("SELECT id, employee_id FROM payroll_run_employees WHERE run_id = :r", r=run_id)}
    docs = {e: payslip_of(r) for e, r in rows.items() if payslip_of(r) is not None}
    assert set(docs) == {pay["emp"]["Emma"]["id"], pay["emp"]["Ivan"]["id"]}
    assert {d[1] for d in docs.values()} == {"GENERATED"}  # nothing submitted yet
    submitted_batch(pay, run_id)
    assert run_status(run_id) == "SUBMITTED"
    assert {payslip_of(rows[e])[1] for e in docs} == {"PUBLISHED"}


# ---------------------------------------------------------------------------
# 3. Concurrent import of the same statement (F-11)
# ---------------------------------------------------------------------------
def _statement(pay, run_id):
    from datetime import date

    ref = sql("SELECT reference FROM payment_instructions WHERE run_id = :r AND employee_id = :e",
              r=run_id, e=pay["emp"]["Emma"]["id"])[0][0]
    return csv_statement((date.today().isoformat(), "-100.00", ref, ""))


def test_f11_concurrent_import_of_the_same_statement_is_a_duplicate_not_a_500(pay):
    """Two real sessions in two threads: A imports and holds its transaction open, B imports the same file,
    then A commits. Before the fix B passed the file_sha256 check (A was not committed) and failed on
    uq_bank_statement_imports_file (HTTP 500); now B waits for A and returns A's import as a duplicate."""
    from app.payroll.services import reconciliation

    cid, fran = pay["cid"], pay["fran"]
    run_id = paid_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    content = _statement(pay, run_id)
    outcome = {}

    def second_import():
        try:
            with session(cid, fran.user_id) as (db, principal):
                outcome["result"] = reconciliation.import_statement(
                    db, principal, funding_account_id=pay["funding"], fmt="CSV", file_name="statement.csv",
                    content=content, mapping=MAPPING)
                db.commit()
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            outcome["error"] = exc

    with session(cid, fran.user_id) as (db, principal):
        first = reconciliation.import_statement(db, principal, funding_account_id=pay["funding"], fmt="CSV",
                                                file_name="statement.csv", content=content, mapping=MAPPING)
        assert first["duplicate"] is False
        thread = threading.Thread(target=second_import, daemon=True)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive()  # B waits for A's lock instead of racing past the duplicate check
        db.commit()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    assert outcome["result"]["duplicate"] is True
    assert outcome["result"]["import"]["id"] == first["import"]["id"]
    assert sql("SELECT count(*) FROM bank_statement_imports WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 1
    assert sql("SELECT count(*) FROM bank_statement_lines WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 1


def test_f11_import_that_loses_the_race_inside_the_window_returns_the_duplicate(pay, monkeypatch):
    """Direct simulation of the race window (the savepoint fallback behind the lock): the duplicate check is
    made to miss the committed import, so the insert hits uq_bank_statement_imports_file; the import returns
    the existing one as a duplicate and the session stays usable."""
    from app.payroll.services import reconciliation

    cid, fran = pay["cid"], pay["fran"]
    run_id = paid_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    content = _statement(pay, run_id)
    with session(cid, fran.user_id) as (db, principal):
        first = reconciliation.import_statement(db, principal, funding_account_id=pay["funding"], fmt="CSV",
                                                file_name="statement.csv", content=content, mapping=MAPPING)
        db.commit()
    real = reconciliation._existing_import
    calls = []

    def blind_once(db, funding, sha):
        calls.append(sha)
        return None if len(calls) == 1 else real(db, funding, sha)

    monkeypatch.setattr(reconciliation, "_existing_import", blind_once)
    with session(cid, fran.user_id) as (db, principal):
        again = reconciliation.import_statement(db, principal, funding_account_id=pay["funding"], fmt="CSV",
                                                file_name="statement.csv", content=content, mapping=MAPPING)
        assert again["duplicate"] is True and again["import"]["id"] == first["import"]["id"]
        db.commit()
    assert len(calls) == 2
    assert sql("SELECT count(*) FROM bank_statement_imports WHERE funding_account_id = :f", f=pay["funding"])[0][0] == 1


# ---------------------------------------------------------------------------
# 4. Separation of duties: the batch approver is not its releaser
# ---------------------------------------------------------------------------
def _approved_file_batch(pay, run_id, approver="alma", **create):
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id, **create}), 201)["batches"][0]
    ok(pay[approver].post(f"{BATCHES}/{batch['id']}/approve"))
    if create.get("method") != "API":
        ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    return batch


def _signatories(pay, *names):
    sql("UPDATE funding_accounts SET signatory_user_ids = CAST(:s AS integer[]) WHERE id = :f",
        s="{" + ",".join(str(pay[n].user_id) for n in names) + "}", f=pay["funding"])


def test_sod_approver_of_a_batch_cannot_release_it_while_separation_of_duties_is_on(pay):
    _signatories(pay, "fran", "alma")
    run_id = paid_run(pay, {"Emma": "100.00"})
    batch = _approved_file_batch(pay, run_id)
    step_up(pay["alma"])
    err(pay["alma"].post(f"{BATCHES}/{batch['id']}/release"), 403, "SEPARATION_OF_DUTIES")
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}
    assert run_status(run_id) == "PAYMENT_READY"
    assert sql("SELECT status, released_by_user_id FROM payment_batches WHERE id = :b", b=batch["id"])[0] == (
        "FILE_GENERATED", None)
    # Another signatory (not the preparer, not the approver) releases it.
    step_up(pay["fran"])
    released = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    assert released["status"] == "SUBMITTED"
    assert sql("SELECT released_by_user_id FROM payment_batches WHERE id = :b", b=batch["id"])[0][0] == pay[
        "fran"].user_id


def test_sod_approver_cannot_release_a_sandbox_api_batch_either(pay):
    _signatories(pay, "fran", "alma")
    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX' WHERE id = :f", f=pay["funding"])
    api_run = paid_run(pay, {"Ivan": "200.00"})
    api_batch = _approved_file_batch(pay, api_run, method="API")
    step_up(pay["alma"])
    err(pay["alma"].post(f"{BATCHES}/{api_batch['id']}/release"), 403, "SEPARATION_OF_DUTIES")
    assert {v["status"] for v in instructions_of(api_run).values()} == {"APPROVED"}
    step_up(pay["fran"])
    assert ok(pay["fran"].post(f"{BATCHES}/{api_batch['id']}/release"))["status"] == "SUBMITTED"


def test_sod_off_lets_the_approver_release_but_never_the_preparer(pay):
    set_setting(pay["cid"], SOD, False)
    _signatories(pay, "fran", "alma", "pete")
    run_id = paid_run(pay, {"Emma": "100.00"})
    batch = _approved_file_batch(pay, run_id)
    step_up(pay["pete"])
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/release"), 403, "DUAL_CONTROL")  # payment dual control stays
    step_up(pay["alma"])
    released = ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/release"))
    assert released["status"] == "SUBMITTED"
    assert sql("SELECT released_by_user_id FROM payment_batches WHERE id = :b", b=batch["id"])[0][0] == pay[
        "alma"].user_id


# ---------------------------------------------------------------------------
# 5. EC-BNK-07: lifting a payment hold needs a fresh step-up
# ---------------------------------------------------------------------------
def _held_duplicate(w):
    emma_account, _ = active_account(w, w["u"]["emma"], in_account())
    verify_with_document(w, emma_account, w["u"]["emma"])
    ivan_account, _ = active_account(w, w["u"]["pia"], in_account("Ivan Test"), url=payee_url(w["emp"]["Ivan"]))
    held = account_row(ivan_account)
    assert (held["payment_hold"], held["hold_reason"]) == (True, "DUPLICATE_FINGERPRINT")
    return emma_account, ivan_account


def _no_step_up(client):
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=bank_user_id(client))


@pytest.mark.parametrize("action", ["CLEAR_REVIEW", "MARK_VERIFIED"])
def test_ec_bnk_07_lifting_a_payment_hold_needs_a_fresh_step_up(bank_world, action):
    w = bank_world
    ava = w["u"]["ava"]
    emma_account, ivan_account = _held_duplicate(w)
    _no_step_up(ava)
    body = {"action": action, "reason": "Joint account of spouses, confirmed"}
    r = ava.post(f"{STAFF}/{ivan_account}/review", json=body)
    assert (r.status_code, r.json().get("code")) == (403, "STEP_UP_REQUIRED"), r.text
    row = account_row(ivan_account)
    assert (row["payment_hold"], row["verification_status"]) == (True, "REQUIRES_REVIEW")
    assert all(f["open"] for f in row["risk_flags"] if f["code"] == "DUPLICATE_ACCOUNT")
    bank_step_up(ava, minutes_ago=10)  # an old step-up is not fresh
    r = ava.post(f"{STAFF}/{ivan_account}/review", json=body)
    assert (r.status_code, r.json().get("code")) == (403, "STEP_UP_REQUIRED"), r.text
    bank_step_up(ava)
    r = ava.post(f"{STAFF}/{ivan_account}/review", json=body)
    if action == "CLEAR_REVIEW":
        assert r.status_code == 200 and r.json()["payment_hold"] is False, r.text
    else:
        # Past the step-up, MARK_VERIFIED still needs the verification methods the policy requires (unchanged).
        assert (r.status_code, r.json().get("code")) == (409, "VERIFICATION_INCOMPLETE"), r.text
        assert account_row(ivan_account)["payment_hold"] is True
    # A review that lifts no hold (the older account of the pair is flagged but not held) needs no step-up.
    assert account_row(emma_account)["payment_hold"] is False
    _no_step_up(ava)
    r = ava.post(f"{STAFF}/{emma_account}/review", json={"action": "CLEAR_REVIEW", "reason": "Joint account, ok"})
    assert r.status_code == 200, r.text
