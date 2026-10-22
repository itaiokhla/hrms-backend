"""Payment batches: proposal, dual control, bank files, release, SANDBOX submission and the run's payment
states (3.7 §7-§8; 3.5 §5.9; EC-PAY-01, EC-PAY-03, EC-PAY-05, EC-PAY-13, EC-PAY-18)."""

import csv
import io
from decimal import Decimal

import pytest

from tests.payroll.test_payment_obligations import (
    BATCHES,
    INSTRUCTIONS,
    NUMBERS,
    add_funding_account,
    err,
    finalized_run,
    instructions_of,
    ok,
    pay,  # noqa: F401  (fixture)
    run_status,
    set_entity_setting,
    step_up,
    submitted_batch,
)
from tests.payroll.test_wage_inputs import audit_actions, set_setting, sql


def test_ec_pay_03_proposal_splits_at_batch_max_items_and_reports_exclusions(pay):
    set_entity_setting(pay, "payroll.payments.batch_max_items", 1)
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00", "Zara": "300.00"}, held=("Zara",))
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert [b["item_count"] for b in proposal["batches"]] == [1, 1]
    assert {b["funding_account_id"] for b in proposal["batches"]} == {pay["funding"]}
    assert [e["reason"] for e in proposal["exclusions"]] == ["EMPLOYEE_HOLD"]
    assert sql("SELECT count(*) FROM payment_batches WHERE run_id = :r", r=run_id)[0][0] == 0  # preview only

    created = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)
    assert [b["status"] for b in created["batches"]] == ["APPROVAL_PENDING", "APPROVAL_PENDING"]
    assert [b["total"] for b in created["batches"]] == [{"amount": "100.00", "currency": "INR"},
                                                       {"amount": "200.00", "currency": "INR"}]
    rows = instructions_of(run_id)
    assert rows[pay["emp"]["Emma"]["id"]]["status"] == "APPROVAL_PENDING"
    assert rows[pay["emp"]["Zara"]["id"]]["status"] == "ON_HOLD" and rows[pay["emp"]["Zara"]["id"]]["batch_id"] is None
    assert "BATCH_CREATED" in audit_actions(pay["cid"], "payment_batch", created["batches"][0]["id"])
    # Nothing is left to batch: a second creation reports it with the exclusions.
    err(pay["pete"].post(BATCHES, json={"run_id": run_id}), 409, "NOTHING_TO_BATCH")


def test_no_funding_account_or_ambiguous_accounts_are_excluded_not_guessed(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    add_funding_account(pay["cid"], pay["le"], [pay["fran"].user_id], number="912020099999999", name="Second")
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert proposal["batches"] == [] and proposal["exclusions"][0]["reason"] == "FUNDING_ACCOUNT_AMBIGUOUS"
    chosen = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id, "funding_account_id": pay["funding"]}))
    assert chosen["batches"][0]["funding_account_id"] == pay["funding"]


def test_dual_control_preparer_cannot_approve_and_approval_moves_the_run_to_payment_ready(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/approve"), 403, "DUAL_CONTROL")
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve", json={"row_version": batch["row_version"] + 5}), 409,
        "ROW_VERSION_CONFLICT")
    approved = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve", json={"row_version": batch["row_version"]}))
    assert approved["status"] == "APPROVED"
    assert {v["status"] for v in instructions_of(run_id).values()} == {"APPROVED"}
    assert run_status(run_id) == "PAYMENT_READY"
    assert "BATCH_APPROVED" in audit_actions(pay["cid"], "payment_batch", batch["id"])
    # Approving twice is refused by the machine.
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"), 409, "INVALID_TRANSITION")


def test_dual_control_off_lets_the_preparer_approve(pay):
    set_setting(pay["cid"], "payroll.approval.payment_dual_control", False)
    run_id = finalized_run(pay, {"Emma": "100.00"})
    batch = ok(pay["fran"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    assert ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))["status"] == "APPROVED"


def test_ec_pay_01_generated_and_downloaded_file_stays_queued_and_is_audited(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"), 409, "INVALID_TRANSITION")  # not approved
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    generated = ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    assert generated["status"] == "FILE_GENERATED" and generated["file"]["record_count"] == 2
    sha = generated["file"]["sha256"]
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}

    first = pay["fran"].get(f"{BATCHES}/{batch['id']}/file")
    assert first.status_code == 200 and first.headers["x-content-sha256"] == sha
    assert "no-store" in first.headers["cache-control"]
    second = pay["pete"].get(f"{BATCHES}/{batch['id']}/file")
    assert second.content == first.content  # deterministic regeneration
    rows = list(csv.reader(io.StringIO(first.content.decode("utf-8"))))
    details = [r for r in rows if r and r[0] == "D"]
    assert len(details) == 2 and NUMBERS["Emma"] in first.content.decode("utf-8")
    trailer = [r for r in rows if r and r[0] == "T"][0]
    assert "92000.50" in trailer and "INR" in trailer
    # EC-PAY-01: a download moves nothing beyond QUEUED; never "Paid".
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}
    detail = ok(pay["pete"].get(f"{BATCHES}/{batch['id']}"))
    assert {i["label"]["text"] for i in detail["instructions"]} == {"Scheduled"}
    assert detail["file"]["download_count"] == 2
    assert audit_actions(pay["cid"], "payment_batch", batch["id"]).count("PAYMENT_FILE_DOWNLOADED") == 2
    assert sql("SELECT storage_path FROM payment_files WHERE batch_id = :b", b=batch["id"])[0][0] is None
    # Bank numbers appear in the file only, never in API responses.
    assert NUMBERS["Emma"] not in str(detail)


def test_regenerated_file_that_differs_fails_loudly_and_blocks_release(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    sql("UPDATE payment_files SET sha256 = :s WHERE batch_id = :b", s="0" * 64, b=batch["id"])
    err(pay["fran"].get(f"{BATCHES}/{batch['id']}/file"), 409, "REGENERATION_MISMATCH")
    step_up(pay["fran"])
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 409, "FILE_HASH_CHANGED")
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}


def test_release_needs_step_up_signatory_and_dual_control_then_is_manual_upload_unverified(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    generated = ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 403, "STEP_UP_REQUIRED")
    step_up(pay["pete"])
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/release"), 403)  # the preparer, and not a signatory
    sql("UPDATE funding_accounts SET signatory_user_ids = ARRAY[:p] WHERE id = :f", p=pay["pete"].user_id,
        f=pay["funding"])
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/release"), 403, "DUAL_CONTROL")
    sql("UPDATE funding_accounts SET signatory_user_ids = ARRAY[:p] WHERE id = :f", p=pay["fran"].user_id,
        f=pay["funding"])
    step_up(pay["fran"])
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release", json={"file_sha256": "a" * 64}), 409,
        "FILE_HASH_CHANGED")
    released = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release",
                                   json={"file_sha256": generated["file"]["sha256"]}))
    assert released["status"] == "SUBMITTED"
    rows = sql("SELECT status, submission_channel, evidence_status, submitted_at IS NOT NULL FROM "
               "payment_instructions WHERE batch_id = :b", b=batch["id"])
    assert {tuple(r) for r in rows} == {("SUBMITTED", "MANUAL_UPLOAD", "UNVERIFIED", True)}
    assert run_status(run_id) == "SUBMITTED"
    detail = ok(pay["fran"].get(f"{BATCHES}/{batch['id']}"))
    assert {i["label"]["text"] for i in detail["instructions"]} == {"Submitted for manual payment — unverified"}
    assert detail["released_by_user_id"] == pay["fran"].user_id and detail["submission_channel"] == "MANUAL_UPLOAD"
    assert "BATCH_RELEASED" in audit_actions(pay["cid"], "payment_batch", batch["id"])
    # EC-PAY-13: a submitted batch is never submitted again (state and file hash).
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"), 409, "BATCH_SUBMITTED")
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/cancel", json={"reason": "Too late"}), 409, "BATCH_SUBMITTED")


def test_ec_pay_13_concurrent_or_replayed_release_is_idempotent(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    step_up(pay["fran"])
    headers = {"Idempotency-Key": "release-once-1"}
    first = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release", headers=headers))
    replay = pay["fran"].post(f"{BATCHES}/{batch['id']}/release", headers=headers)
    assert replay.status_code == 200 and replay.json() == first
    assert sql("SELECT count(*) FROM payment_events e JOIN payment_instructions i ON i.id = e.instruction_id "
               "WHERE i.batch_id = :b AND e.to_status = 'SUBMITTED'", b=batch["id"])[0][0] == 1


def test_reject_and_cancel_return_payments_to_ready_for_another_batch(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    err(pay["fran"].post(f"{BATCHES}/{batch['id']}/reject", json={"reason": " "}), 400, "REASON_REQUIRED")
    rejected = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/reject", json={"reason": "Wrong funding account"}))
    assert rejected["status"] == "CANCELLED"
    row = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]
    assert (row["status"], row["batch_id"]) == ("READY", None)
    second = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{second['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{second['id']}/generate-file"))
    cancelled = ok(pay["pete"].post(f"{BATCHES}/{second['id']}/cancel", json={"reason": "Value date moved"}))
    assert cancelled["status"] == "CANCELLED"
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "READY"
    assert "BATCH_CANCELLED" in audit_actions(pay["cid"], "payment_batch", second["id"])


def test_bank_file_formats_that_are_not_offered_are_refused(pay):
    set_entity_setting(pay, "payroll.payments.bank_file_format", "AE_WPS_SIF")
    run_id = finalized_run(pay, {"Emma": "100.00"})
    err(pay["pete"].post(BATCHES, json={"run_id": run_id}), 409, "FORMAT_NOT_OFFERED")
    set_entity_setting(pay, "payroll.payments.bank_file_format", "US_NACHA")
    err(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}), 409, "FORMAT_NOT_OFFERED")


def test_ec_pay_05_sandbox_api_batch_uses_the_idempotency_key_and_is_labelled_sandbox(pay):
    from app.payroll.services import payment_adapters as adapters

    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX' WHERE id = :f", f=pay["funding"])
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "250.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id, "method": "API"}), 201)["batches"][0]
    assert batch["sandbox"] is True and batch["method"] == "API"
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"), 409, "NOT_A_FILE_BATCH")
    step_up(pay["fran"])
    released = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    assert released["status"] == "SUBMITTED"
    rows = sql("SELECT id, idempotency_key, provider_ref, submission_channel, status FROM payment_instructions "
               "WHERE batch_id = :b ORDER BY id", b=batch["id"])
    for _id, key, ref, channel, status in rows:
        assert (channel, status) == ("SANDBOX", "SUBMITTED")
        assert ref == adapters.sandbox_reference(key)  # a retry with the same key gives the same reference
    detail = ok(pay["fran"].get(f"{BATCHES}/{batch['id']}"))
    assert detail["badge"] == "SANDBOX" and {i["label"]["badge"] for i in detail["instructions"]} == {"SANDBOX"}
    assert run_status(run_id) == "SUBMITTED"
    assert "SANDBOX_BATCH_SUBMITTED" in audit_actions(pay["cid"], "payment_batch", batch["id"])
    # Re-submitting the same instructions to the sandbox returns the same references (EC-PAY-05).
    items = [adapters.SubmissionItem(r[0], r[1], Decimal("100.00"), "INR") for r in rows]
    again = adapters.sandbox_submit(adapters.ENDPOINTS["SANDBOX"]["default"], items)
    assert [r.provider_ref for r in again] == [r[2] for r in rows]


def test_ec_pay_18_payout_endpoints_come_only_from_the_allow_list(pay):
    from app.payroll.services import payment_adapters as adapters

    for profile in ("https://evil.example/payouts", "SANDBOX:https", "SANDBOX:other", "//evil", "user@host"):
        with pytest.raises(adapters.AdapterError) as info:
            adapters.resolve_endpoint(profile)
        assert info.value.code in ("ENDPOINT_NOT_ALLOWED", "ADAPTER_UNKNOWN")
    with pytest.raises(adapters.AdapterError):
        adapters.sandbox_submit("https://evil.example/payouts", [])
    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX:other' WHERE id = :f", f=pay["funding"])
    run_id = finalized_run(pay, {"Emma": "100.00"})
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id, "method": "API"}))
    assert proposal["batches"] == [] and proposal["exclusions"][0]["reason"] == "NO_PAYOUT_PROVIDER"


def test_instruction_events_are_listed_and_unbatched_payments_can_be_cancelled(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    row = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]
    history = ok(pay["pete"].get(f"{INSTRUCTIONS}/{row['id']}/events"))
    assert [e["event_status"] for e in history["events"]] == ["CREATED"]
    cancelled = ok(pay["pete"].post(f"{INSTRUCTIONS}/{row['id']}/cancel", json={"reason": "Paid by cheque later"}))
    assert cancelled["status"] == "CANCELLED"
    assert sql("SELECT status FROM payment_obligations WHERE id = (SELECT obligation_id FROM payment_instructions "
               "WHERE id = :i)", i=row["id"])[0][0] == "CANCELLED"
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ivan = instructions_of(run_id)[pay["emp"]["Ivan"]["id"]]
    err(pay["pete"].post(f"{INSTRUCTIONS}/{ivan['id']}/cancel", json={"reason": "No"}), 409, "IN_BATCH")
    listed = ok(pay["pete"].get(INSTRUCTIONS, params={"batch_id": batch["id"]}))
    assert listed["total"] == 1


def test_batch_balances_per_batch(pay):
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"})
    batch = submitted_batch(pay, run_id)
    balances = ok(pay["pete"].get("/api/v1/payroll/payment-balances", params={"group_by": "batch",
                                                                              "run_id": run_id}))["items"]
    (item,) = balances
    assert item["batch_id"] == batch["id"]
    assert item["intended"]["amount"] == "300.00" and item["submitted"]["amount"] == "300.00"
    assert item["settled"]["amount"] == "0.00" and item["outstanding"]["amount"] == "300.00"
