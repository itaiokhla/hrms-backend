"""Adversarial review (payments and banking security): a payment that was held after it joined a batch never
leaves through that batch (EC-RUN-13, EC-BNK-03 risk holds, run reversals), a reversal hold is never lifted by a
person, and the people allowed to release a funding account's batches change only after a step-up.

Each test demonstrates a defect found in review: before the fix the held payment was written into the bank
file (with the full account number) or released, and a reversal hold could be released by an approver.
"""

from types import SimpleNamespace

from tests.payroll.test_payment_obligations import (
    BATCHES,
    INSTRUCTIONS,
    NUMBERS,
    err,
    finalized_run,
    instructions_of,
    ok,
    pay,  # noqa: F401  (fixture)
    run_status,
    session,
    step_up,
)
from tests.payroll.test_wage_inputs import audit_actions, sql

RUNS = "/api/v1/payroll/runs"
FUNDING = "/api/v1/payroll/funding-accounts"


def _run_employee(run_id, employee_id):
    return sql("SELECT id FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e",
               r=run_id, e=employee_id)[0][0]


def _approved_batch(world, run_id):
    batch = ok(world["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(world["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    return batch


def test_ec_run_13_payment_hold_after_file_generation_blocks_download_and_release(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = _approved_batch(pay, run_id)
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    assert run_status(run_id) == "PAYMENT_READY"
    ivan_row = _run_employee(run_id, pay["emp"]["Ivan"]["id"])
    # Finance puts Ivan's payment on hold while the run waits for payment (allowed in PAYMENT_READY).
    ok(pay["fran"].post(f"{RUNS}/{run_id}/employees/{ivan_row}/hold",
                         json={"hold_type": "PAYMENT", "reason_code": "INVESTIGATION", "note": "Fraud check"}))

    # The file holding Ivan's full account number is no longer handed out, and the batch cannot be released.
    blocked = err(pay["pete"].get(f"{BATCHES}/{batch['id']}/file"), 409, "PAYMENTS_HELD")
    assert NUMBERS["Ivan"] not in str(blocked)
    step_up(pay["fran"])
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 409, "PAYMENTS_HELD")
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}
    assert run_status(run_id) == "PAYMENT_READY"

    # Cancelling the batch frees the payments; the next proposal holds Ivan and batches Emma only.
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/cancel", json={"reason": "Ivan is on hold"}))
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    rows = instructions_of(run_id)
    assert proposal["batches"][0]["instruction_ids"] == [rows[pay["emp"]["Emma"]["id"]]["id"]]
    assert [e["reason"] for e in proposal["exclusions"]] == ["EMPLOYEE_HOLD"]


def test_ec_run_13_payment_hold_after_approval_blocks_file_generation(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = _approved_batch(pay, run_id)
    ivan_row = _run_employee(run_id, pay["emp"]["Ivan"]["id"])
    ok(pay["fran"].post(f"{RUNS}/{run_id}/employees/{ivan_row}/hold",
                         json={"hold_type": "PAYMENT", "reason_code": "DISPUTE", "note": "Disputed pay"}))
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"), 409, "PAYMENTS_HELD")
    assert sql("SELECT count(*) FROM payment_files WHERE batch_id = :b", b=batch["id"])[0][0] == 0
    assert {v["status"] for v in instructions_of(run_id).values()} == {"APPROVED"}


def test_reversal_hold_on_a_batched_payment_never_reaches_the_bank_file_and_is_not_released_by_a_person(pay):
    from app.payroll.services import reversals

    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = _approved_batch(pay, run_id)
    ivan_row = _run_employee(run_id, pay["emp"]["Ivan"]["id"])
    # An approved run reversal of Ivan holds his unsent payment (the reversals service's own step).
    with session(pay["cid"]) as (db, _):
        held = reversals._hold_unsent(db, SimpleNamespace(id=1, company_id=pay["cid"], run_employee_ids=[ivan_row]))
        db.commit()
    ivan = instructions_of(run_id)[pay["emp"]["Ivan"]["id"]]
    assert held == [ivan["id"]] and (ivan["status"], ivan["reason"]) == ("ON_HOLD", "RUN_REVERSAL")

    # The held (soon cancelled) payment must not be written into the file the bank will pay.
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"), 409, "PAYMENTS_HELD")
    assert sql("SELECT count(*) FROM payment_files WHERE batch_id = :b", b=batch["id"])[0][0] == 0
    step_up(pay["fran"])
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 409)

    # Cancelling the batch detaches the held payment too (it is not stuck in a cancelled batch).
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/cancel", json={"reason": "Run reversed for Ivan"}))
    ivan = instructions_of(run_id)[pay["emp"]["Ivan"]["id"]]
    assert (ivan["status"], ivan["reason"], ivan["batch_id"]) == ("ON_HOLD", "RUN_REVERSAL", None)
    # A reversal hold is lifted only by the reversal (cancelled when it is finalized), never by a person.
    err(pay["fran"].post(f"{INSTRUCTIONS}/{ivan['id']}/release-hold", json={"reason": "Pay him anyway"}), 409,
        "REVERSAL_HOLD")
    assert instructions_of(run_id)[pay["emp"]["Ivan"]["id"]]["status"] == "ON_HOLD"

    # The next batch pays Emma only; Ivan's number is not in its file.
    again = _approved_batch(pay, run_id)
    assert again["item_count"] == 1
    ok(pay["pete"].post(f"{BATCHES}/{again['id']}/generate-file"))
    content = pay["pete"].get(f"{BATCHES}/{again['id']}/file").content.decode("utf-8")
    assert NUMBERS["Emma"] in content and NUMBERS["Ivan"] not in content


def test_ec_bnk_risk_hold_on_an_account_after_batching_blocks_release_and_rebatching(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = _approved_batch(pay, run_id)
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    # A duplicate fingerprint found later puts Ivan's account on a payment hold (bank-accounts service).
    sql("UPDATE bank_accounts SET payment_hold = true, hold_reason = 'DUPLICATE_FINGERPRINT', "
        "row_version = row_version + 1 WHERE company_id = :c AND employee_id = :e", c=pay["cid"],
        e=pay["emp"]["Ivan"]["id"])
    step_up(pay["fran"])
    held = err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 409, "PAYMENTS_HELD")
    assert "ACCOUNT_REVIEW" in held["detail"]
    err(pay["pete"].get(f"{BATCHES}/{batch['id']}/file"), 409, "PAYMENTS_HELD")
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}

    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/cancel", json={"reason": "Account under review"}))
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    rows = instructions_of(run_id)
    assert proposal["batches"][0]["instruction_ids"] == [rows[pay["emp"]["Emma"]["id"]]["id"]]
    assert {e["employee_id"]: e["reason"] for e in proposal["exclusions"]} == {
        pay["emp"]["Ivan"]["id"]: "ACCOUNT_REVIEW"}
    assert (rows[pay["emp"]["Ivan"]["id"]]["status"], rows[pay["emp"]["Ivan"]["id"]]["reason"]) == (
        "ON_HOLD", "ACCOUNT_REVIEW")


def test_unheld_batch_still_releases_after_the_checks(pay):
    """Positive path: with nothing held, generation, download and release work as before."""
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = _approved_batch(pay, run_id)
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    assert pay["fran"].get(f"{BATCHES}/{batch['id']}/file").status_code == 200
    step_up(pay["fran"])
    released = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    assert released["status"] == "SUBMITTED" and run_status(run_id) == "SUBMITTED"
    assert {v["status"] for v in instructions_of(run_id).values()} == {"SUBMITTED"}


def test_funding_signatories_and_payout_provider_change_only_after_step_up(pay):
    url = f"{FUNDING}/{pay['funding']}"
    fran = pay["fran"]  # finance_payments: payroll:funding_accounts:manage
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=fran.user_id)
    # Who may release batches from the account is a payment-diversion control: a step-up is required.
    err(fran.patch(url, json={"signatory_user_ids": [pay["pete"].user_id], "row_version": 1}), 403,
        "STEP_UP_REQUIRED")
    err(fran.patch(url, json={"provider_profile": "SANDBOX", "row_version": 1}), 403, "STEP_UP_REQUIRED")
    assert sql("SELECT signatory_user_ids FROM funding_accounts WHERE id = :f", f=pay["funding"])[0][0] == [
        pay["fran"].user_id]
    # A plain rename still needs no step-up.
    renamed = ok(fran.patch(url, json={"name": "Payroll main", "row_version": 1}))
    step_up(fran)
    updated = ok(fran.patch(url, json={"signatory_user_ids": [pay["fran"].user_id, pay["pete"].user_id],
                                        "row_version": renamed["row_version"]}))
    assert sorted(updated["signatory_user_ids"]) == sorted([pay["fran"].user_id, pay["pete"].user_id])
    assert "FUNDING_ACCOUNT_UPDATED" in audit_actions(pay["cid"], "funding_account", pay["funding"])
