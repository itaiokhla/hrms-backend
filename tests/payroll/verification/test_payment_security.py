"""Goal 2 §12 security: payment controls under attack (sandbox/test adapters only, no real money or banks).

Duplicate payouts, replayed and forged webhooks, mismatched settlement amounts, a beneficiary changed after the
batch, missing approval, unauthorized finance roles, genuinely concurrent batch creation/approval/release and
webhook delivery, retries after a timeout. Every expected amount is written out by hand in the test.

World: ``payments_world`` of tests/payroll/test_payment_obligations.py (one tenant, LE01, pay group PG-1, finance
users Pete (prepares), Alma (approves), Fran (releases; the funding account's only signatory), a funding
account) and ``finalized_run`` (a run moved to FINALIZED whose ``payroll.run.finalized`` event creates the
obligations and instructions through the real subscribers). Bank numbers are fictitious test values.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal

import pytest

from tests.helpers import PASSWORD, create_employee, new_client
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import (
    BATCHES,
    INSTRUCTIONS,
    OBLIGATIONS,
    add_bank_account,
    end_bank_account,
    finalized_run,
    instructions_of,
    payments_world,
    run_status,
    step_up,
)
from tests.payroll.test_reconciliation import csv_statement, upload
from tests.payroll.test_wage_inputs import sql

WEBHOOK = "/api/v1/webhooks/payments/SANDBOX"


@pytest.fixture
def pay(app):
    return payments_world(app, "Payments Security Co")


def ok(response, status=200):
    assert response.status_code == status, f"{response.status_code}: {response.text[:400]}"
    return response.json()


def code_of(response):
    try:
        return response.json().get("code")
    except ValueError:
        return None


def second_session(app, client):
    """A second signed-in session of the same user (another browser), stepped up."""
    other = new_client(app)
    r = other.post("/api/v1/auth/login", json={"email": client.email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    other.email, other.user_id = client.email, client.user_id
    return other


def live_instructions(run_id):
    return sql("SELECT id, obligation_id, batch_id, amount, status FROM payment_instructions WHERE run_id = :r "
               "AND status NOT IN ('CANCELLED', 'REISSUED') ORDER BY id", r=run_id)


def allocation_holds(run_id):
    """Allocations never exceed obligations: per obligation, live instruction amounts <= the obligation amount."""
    rows = sql("SELECT o.id, o.amount, coalesce(sum(i.amount) FILTER (WHERE i.status NOT IN ('CANCELLED', "
               "'REISSUED')), 0) FROM payment_obligations o LEFT JOIN payment_instructions i ON i.obligation_id = o.id "
               "WHERE o.run_id = :r GROUP BY o.id, o.amount", r=run_id)
    return all(Decimal(allocated) <= Decimal(amount) for _, amount, allocated in rows), rows


def submitted_events(batch_id):
    return sql("SELECT i.id, count(*) FROM payment_events e JOIN payment_instructions i ON i.id = e.instruction_id "
               "WHERE i.batch_id = :b AND e.to_status = 'SUBMITTED' GROUP BY i.id", b=batch_id)


def run_parallel(*calls):
    """Start every call at the same moment on its own thread; returns their results in order."""
    results = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def worker(i, call):
        barrier.wait()
        try:
            results[i] = call()
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


# ---------------------------------------------------------------------------
# Duplicate payouts (EC-PAY-13, EC-PAY-15)
# ---------------------------------------------------------------------------
def test_ec_pay_15_repeated_finalized_events_and_a_second_batch_never_create_a_second_payout(pay):
    """EC-PAY-15, EC-PAY-13: the finalized-run event delivered three times creates one obligation per employee
    (Emma 1000.00 + Ivan 2000.00 = 3000.00 obligated); a second batch for the same run takes nothing (every
    instruction is in exactly one live batch) and allocations never exceed obligations."""
    from app.payroll.services import outbox
    from tests.payroll.test_wage_inputs import session

    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"})
    for _ in range(2):
        with session(pay["cid"]) as (db, _):
            outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
            db.commit()
    obligations = sql("SELECT employee_id, amount FROM payment_obligations WHERE run_id = :r ORDER BY amount",
                      r=run_id)
    assert [(e, Decimal(a)) for e, a in obligations] == [(pay["emp"]["Emma"]["id"], Decimal("1000.00")),
                                                        (pay["emp"]["Ivan"]["id"], Decimal("2000.00"))]
    assert sum(Decimal(a) for _, a in obligations) == Decimal("3000.00")
    assert len(live_instructions(run_id)) == 2

    first = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"]
    assert len(first) == 1 and first[0]["item_count"] == 2
    second = pay["pete"].post(BATCHES, json={"run_id": run_id})
    assert second.status_code in (201, 409), second.text
    if second.status_code == 201:
        assert all(b["item_count"] == 0 for b in second.json()["batches"]), second.text
    rows = live_instructions(run_id)
    assert {r[2] for r in rows} == {first[0]["id"]} and len(rows) == 2
    holds, detail = allocation_holds(run_id)
    assert holds, detail


def test_ec_pay_13_concurrent_batch_creation_puts_each_payment_in_exactly_one_batch(app, pay):
    """EC-PAY-13 (concurrent submission): two sessions of the preparer create batches for the same run at the
    same moment: every payment ends in exactly one live batch and nothing is allocated twice."""
    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"})
    other = second_session(app, pay["pete"])
    results = run_parallel(lambda: pay["pete"].post(BATCHES, json={"run_id": run_id}),
                           lambda: other.post(BATCHES, json={"run_id": run_id}))
    assert not [r for r in results if isinstance(r, Exception)], results
    assert all(r.status_code in (201, 409) for r in results), [r.text for r in results]
    rows = live_instructions(run_id)
    assert len(rows) == 2 and len({r[2] for r in rows}) == 1 and rows[0][2] is not None, rows
    batches_with_items = sql("SELECT b.id FROM payment_batches b WHERE b.company_id = :c AND EXISTS (SELECT 1 FROM "
                             "payment_instructions i WHERE i.batch_id = b.id)", c=pay["cid"])
    assert len(batches_with_items) == 1
    holds, detail = allocation_holds(run_id)
    assert holds, detail


def test_ec_pay_13_concurrent_approval_and_release_submit_each_payment_once(app, pay):
    """EC-PAY-13: two approvers approve the same batch at once (one approval recorded), then two sessions of the
    signatory release it at once: exactly one SUBMITTED event per payment; a retry with the same
    Idempotency-Key after a timeout replays the stored answer and submits nothing again."""
    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    approver2 = login_as(app, pay["admin"], create_employee(pay["admin"], "Abe", "Approver",
                                                            hire_date="2024-01-15"), ["finance_payments"])
    results = run_parallel(lambda: pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"),
                           lambda: approver2.post(f"{BATCHES}/{batch['id']}/approve"))
    statuses = sorted(r.status_code for r in results)
    assert statuses[0] == 200 and statuses[1] in (200, 409), [r.text for r in results]
    approvals = sql("SELECT count(*) FROM audit_logs WHERE company_id = :c AND entity_type = 'payment_batch' AND "
                    "entity_id = :b AND action LIKE '%APPROVED%'", c=pay["cid"], b=str(batch["id"]))[0][0]
    assert approvals == 1, approvals
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    fran2 = second_session(app, pay["fran"])
    step_up(pay["fran"])
    results = run_parallel(lambda: pay["fran"].post(f"{BATCHES}/{batch['id']}/release"),
                           lambda: fran2.post(f"{BATCHES}/{batch['id']}/release"))
    codes = sorted((r.status_code, code_of(r)) for r in results)
    assert codes[0][0] == 200 and codes[1] in ((200, None), (409, "BATCH_SUBMITTED")), [r.text for r in results]
    assert sorted(n for _, n in submitted_events(batch["id"])) == [1, 1]
    # Retry after a client timeout: same key -> the stored answer, no second submission.
    key = {"Idempotency-Key": "verify-release-retry-1"}
    first = pay["fran"].post(f"{BATCHES}/{batch['id']}/release", headers=key)
    again = pay["fran"].post(f"{BATCHES}/{batch['id']}/release", headers=key)
    assert (first.status_code, first.json()) == (again.status_code, again.json())
    assert sorted(n for _, n in submitted_events(batch["id"])) == [1, 1]


# ---------------------------------------------------------------------------
# Missing approval, separation of duties, unauthorized finance roles (EC-SEC-10)
# ---------------------------------------------------------------------------
def test_missing_approval_dual_control_and_unauthorized_roles_cannot_move_money(app, pay):
    """Goal 2 §12 (missing approval, unauthorized finance role) and EC-SEC-10: an unapproved batch has no file
    and cannot be released; the preparer cannot approve their own batch; the approver cannot release what they
    approved; a non-signatory cannot release; payroll processors, approvers and employees get 403 on every
    payment action; nothing reaches SUBMITTED."""
    run_id = finalized_run(pay, {"Emma": "1000.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    url = f"{BATCHES}/{batch['id']}"
    r = pay["pete"].post(f"{url}/generate-file")
    assert r.status_code == 409, r.text
    step_up(pay["fran"])
    r = pay["fran"].post(f"{url}/release")
    assert r.status_code == 409, r.text  # the signatory, stepped up, but the batch is not approved
    assert pay["fran"].get(f"{url}/file").status_code in (404, 409)
    r = pay["pete"].post(f"{url}/approve")
    assert r.status_code == 403 and code_of(r) == "DUAL_CONTROL", r.text
    processor = login_as(app, pay["admin"], create_employee(pay["admin"], "Proc", "Essor", hire_date="2024-01-15"),
                         ["payroll_processor"])
    approver = login_as(app, pay["admin"], create_employee(pay["admin"], "Appr", "Over", hire_date="2024-01-15"),
                        ["payroll_approver"])
    employee = login_as(app, pay["admin"], pay["emp"]["Emma"], ["employee"])
    for client in (processor, approver, employee):
        step_up(client)
        for method, target, body in (("POST", BATCHES, {"run_id": run_id}), ("POST", f"{url}/approve", {}),
                                     ("POST", f"{url}/generate-file", {}), ("POST", f"{url}/release", {}),
                                     ("POST", f"{url}/cancel", {"reason": "x"}), ("GET", OBLIGATIONS, None)):
            r = client.request(method, target, json=body) if body is not None else client.request(method, target)
            assert r.status_code == 403 and code_of(r) == "FORBIDDEN", (client.email, target, r.text)
    ok(pay["alma"].post(f"{url}/approve"))
    ok(pay["pete"].post(f"{url}/generate-file"))
    # The approver is also made a signatory: separation of duties still refuses the release.
    sql("UPDATE funding_accounts SET signatory_user_ids = ARRAY[:a, :f] WHERE id = :id", a=pay["alma"].user_id,
        f=pay["fran"].user_id, id=pay["funding"])
    step_up(pay["alma"])
    r = pay["alma"].post(f"{url}/release")
    assert r.status_code == 403 and code_of(r) == "SEPARATION_OF_DUTIES", r.text
    step_up(pay["pete"])
    r = pay["pete"].post(f"{url}/release")
    assert r.status_code == 403, r.text  # the preparer is no signatory
    assert {v["status"] for v in instructions_of(run_id).values()} == {"QUEUED"}
    released = ok(pay["fran"].post(f"{url}/release"))
    assert released["status"] == "SUBMITTED"


# ---------------------------------------------------------------------------
# Beneficiary changed after the batch (EC-BNK-03)
# ---------------------------------------------------------------------------
def test_ec_bnk_03_beneficiary_changed_after_approval_is_never_silently_paid_to_the_new_account(pay):
    """EC-BNK-03 (Goal 2 §12 "different beneficiary"): Emma's account is replaced after her payment was approved.
    The bank file pays the approved snapshot (old number), never the new account; finance is warned on the batch;
    recreating the payment for the new account needs a new batch and a new approval."""
    old_number, new_number = "771500001111", "771500009999"
    emma = pay["emp"]["Emma"]["id"]
    old = add_bank_account(pay["cid"], emma, old_number, holder="Emma Test")
    run_id = finalized_run(pay, {"Emma": "1500.00"}, with_accounts=False)
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    new = add_bank_account(pay["cid"], emma, new_number, holder="Emma Test", replaces=old)
    end_bank_account(old, replaced_by=new)

    detail = ok(pay["pete"].get(f"{BATCHES}/{batch['id']}"))
    assert detail["bank_changes"], "finance is warned about the changed beneficiary"
    assert old_number not in json.dumps(detail) and new_number not in json.dumps(detail)  # masked
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    file = pay["pete"].get(f"{BATCHES}/{batch['id']}/file")
    assert file.status_code == 200, file.text[:300]
    content = file.content.decode("utf-8", "replace")
    assert old_number in content and new_number not in content
    assert "1500.00" in content  # the approved amount, unchanged
    instruction = instructions_of(run_id)[emma]
    assert sql("SELECT bank_account_id FROM payment_instructions WHERE id = :i", i=instruction["id"])[0][0] == old


# ---------------------------------------------------------------------------
# Webhooks: replayed, forged, mismatched (EC-PAY-06, EC-PAY-08) and statements (EC-PAY-11)
# ---------------------------------------------------------------------------
def _deliver(app, payload, *, secret=None, signature=None, timestamp=None, raw=None):
    from app.payroll.services import payment_adapters as adapters

    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    ts = int(time.time()) if timestamp is None else timestamp
    header = signature if signature is not None else adapters.sign(body, ts, secret)
    return new_client(app).http.post(WEBHOOK, content=body, headers={adapters.SIGNATURE_HEADER: header,
                                                                     "Content-Type": "application/json"})


def _sandbox_payment(pay, net="1000.00"):
    sql("UPDATE funding_accounts SET provider_profile = 'SANDBOX' WHERE id = :f", f=pay["funding"])
    run_id = finalized_run(pay, {"Emma": net})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id, "method": "API"}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    step_up(pay["fran"])
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    instruction = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    ref = sql("SELECT provider_ref FROM payment_instructions WHERE id = :i", i=instruction)[0][0]
    assert ref, "the sandbox adapter assigned a provider reference"
    return run_id, instruction, ref


def _status(instruction_id):
    return tuple(sql("SELECT status, evidence_status FROM payment_instructions WHERE id = :i", i=instruction_id)[0])


def test_ec_pay_08_forged_tampered_replayed_and_mismatched_webhooks_never_settle(app, pay):
    """EC-PAY-08, EC-PAY-06, EC-MON-18: a wrong secret, a captured signature over a modified body and a stale
    timestamp are refused (401) and change nothing; a settlement in another currency is a CONFLICT; a settlement
    for 999.99 against a 1000.00 payment is only PARTIALLY_SETTLED (outstanding 1000.00 - 999.99 = 0.01, the run
    is not paid); the genuine full event delivered twice at the same moment is applied once, and a replay of its
    id with another status is a duplicate."""
    run_id, instruction, ref = _sandbox_payment(pay, "1000.00")
    assert _status(instruction) == ("SUBMITTED", "UNVERIFIED")
    genuine = {"id": "evt_verify_settle_1", "provider_ref": ref, "status": "SETTLED", "amount": "1000.00",
               "currency": "INR"}
    forged = _deliver(app, genuine, secret=b"z" * 32)
    assert forged.status_code == 401 and code_of(forged) == "SIGNATURE_INVALID"
    from app.payroll.services import payment_adapters as adapters

    ts = int(time.time())
    captured_sig = adapters.sign(json.dumps(genuine).encode("utf-8"), ts, None)
    tampered = dict(genuine, amount="1.00")
    r = _deliver(app, None, raw=json.dumps(tampered).encode("utf-8"), signature=captured_sig, timestamp=ts)
    assert r.status_code == 401 and code_of(r) == "SIGNATURE_INVALID", r.text
    r = _deliver(app, genuine, timestamp=ts - 3600)
    assert r.status_code == 401 and code_of(r) == "TIMESTAMP_OUT_OF_WINDOW", r.text
    r = _deliver(app, {"id": "evt_verify_ccy_1", "provider_ref": ref, "status": "SETTLED", "amount": "1000.00",
                       "currency": "USD"})
    assert r.status_code == 200 and r.json() == {"outcome": "CONFLICT"}, r.text
    assert _status(instruction) == ("SUBMITTED", "UNVERIFIED")

    r = _deliver(app, {"id": "evt_verify_short_1", "provider_ref": ref, "status": "SETTLED", "amount": "999.99",
                       "currency": "INR"})
    assert r.status_code == 200 and r.json() == {"outcome": "APPLIED"}, r.text
    assert _status(instruction) == ("PARTIALLY_SETTLED", "VERIFIED")
    assert Decimal(sql("SELECT settled_amount FROM payment_instructions WHERE id = :i", i=instruction)[0][0]) ==         Decimal("999.99")
    outstanding = ok(pay["fran"].get(OBLIGATIONS, params={"run_id": run_id}))["items"][0]["outstanding"]
    assert outstanding == {"amount": "0.01", "currency": "INR"}
    assert run_status(run_id) != "PAID"

    results = run_parallel(lambda: _deliver(app, genuine), lambda: _deliver(app, genuine))
    assert not [r for r in results if isinstance(r, Exception)], results
    outcomes = sorted(r.json()["outcome"] for r in results)
    assert outcomes == ["APPLIED", "DUPLICATE"], [r.text for r in results]
    assert _status(instruction) == ("SETTLED", "VERIFIED")
    assert Decimal(sql("SELECT settled_amount FROM payment_instructions WHERE id = :i", i=instruction)[0][0]) ==         Decimal("1000.00")
    assert sql("SELECT count(*) FROM payment_events WHERE external_event_id = 'evt_verify_settle_1'")[0][0] == 1
    r = _deliver(app, dict(genuine, status="RETURNED"))
    assert r.json() == {"outcome": "DUPLICATE"} and _status(instruction) == ("SETTLED", "VERIFIED")
    assert run_status(run_id) == "PAID"


def test_ec_mon_18_a_webhook_settling_more_than_was_paid_is_never_recorded_as_settled(app, pay):
    """EC-MON-18 / EC-PAY-11 (Goal 2 §12 "mismatched amounts"). Regression for the defect found on 22 Oct 2026:
    apply_evidence only downgraded amount < payout to PARTIALLY_SETTLED, so a provider reporting 1000.01 settled
    for a 1000.00 payment was applied as SETTLED with settled_amount 1000.01 (outstanding 1000.00 - 1000.01 =
    -0.01). Nothing was paid beyond 1000.00: the event is a CONFLICT kept in the history for review, the
    instruction stays SUBMITTED with unverified evidence, the obligation still shows 1000.00 outstanding
    (1000.00 - 0.00) and the run is not paid. The genuine 1000.00 settlement afterwards still applies."""
    run_id, instruction, ref = _sandbox_payment(pay, "1000.00")
    r = _deliver(app, {"id": "evt_verify_over_1", "provider_ref": ref, "status": "SETTLED", "amount": "1000.01",
                       "currency": "INR"})
    assert r.status_code == 200, r.text
    assert r.json() == {"outcome": "CONFLICT"}, r.text
    settled = sql("SELECT settled_amount FROM payment_instructions WHERE id = :i", i=instruction)[0][0]
    assert settled is None or Decimal(settled) <= Decimal("1000.00"), f"settled {settled} for a 1000.00 payment"
    assert _status(instruction) == ("SUBMITTED", "UNVERIFIED")
    outstanding = ok(pay["fran"].get(OBLIGATIONS, params={"run_id": run_id}))["items"][0]["outstanding"]
    assert outstanding == {"amount": "1000.00", "currency": "INR"}, outstanding
    assert run_status(run_id) != "PAID"
    kept = sql("SELECT outcome, from_status, to_status, details::text FROM payment_events "
               "WHERE external_event_id = 'evt_verify_over_1'")
    assert len(kept) == 1 and kept[0][:3] == ("CONFLICT", "SUBMITTED", "SUBMITTED"), kept
    assert "AMOUNT_EXCEEDS_PAYMENT" in kept[0][3]
    # The same event id again is a duplicate; the genuine full settlement still applies once.
    again = _deliver(app, {"id": "evt_verify_over_1", "provider_ref": ref, "status": "SETTLED", "amount": "1000.01",
                           "currency": "INR"})
    assert again.json() == {"outcome": "DUPLICATE"}, again.text
    genuine = _deliver(app, {"id": "evt_verify_over_2", "provider_ref": ref, "status": "SETTLED",
                             "amount": "1000.00", "currency": "INR"})
    assert genuine.json() == {"outcome": "APPLIED"}, genuine.text
    assert _status(instruction) == ("SETTLED", "VERIFIED")
    assert Decimal(sql("SELECT settled_amount FROM payment_instructions WHERE id = :i", i=instruction)[0][0]) ==         Decimal("1000.00")
    assert run_status(run_id) == "PAID"


def test_ec_pay_11_statement_amount_one_paisa_short_is_never_auto_settled(pay):
    """EC-PAY-11, EC-MON-18: a statement debit of 999.99 for a 1000.00 payment (reference matching) does not
    settle it automatically; the instruction stays SUBMITTED with unverified evidence and the run unpaid."""
    run_id = finalized_run(pay, {"Emma": "1000.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    step_up(pay["fran"])
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    instruction = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    reference = sql("SELECT reference FROM payment_instructions WHERE id = :i", i=instruction)[0][0]
    from datetime import date

    statement = upload(pay["fran"], pay["funding"], csv_statement((date.today().isoformat(), "-999.99", reference, "")))
    assert all(m["status"] not in ("MATCHED", "AUTO_MATCHED", "SETTLED") for m in statement["matches"]), statement
    assert _status(instruction) == ("SUBMITTED", "UNVERIFIED")
    assert run_status(run_id) == "SUBMITTED"
    outstanding = ok(pay["fran"].get(f"{OBLIGATIONS}", params={"run_id": run_id}))["items"][0]["outstanding"]
    assert outstanding == {"amount": "1000.00", "currency": "INR"}  # nothing settled: 1000.00 - 0.00


def test_payment_detail_endpoints_never_expose_full_numbers_or_secrets(pay):
    """Goal 2 §12 masking on the payment surfaces: instruction, events, obligations and batch detail show masked
    beneficiaries only; the sealed snapshot is never returned; responses are not cacheable."""
    run_id = finalized_run(pay, {"Emma": "1000.00"})
    emma_number = "501001234567"  # tests/payroll/test_payment_obligations.py NUMBERS["Emma"] (fictitious)
    instruction = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    for url in (f"{INSTRUCTIONS}/{instruction}", f"{INSTRUCTIONS}/{instruction}/events", OBLIGATIONS,
                f"{BATCHES}/{batch['id']}", BATCHES, f"/api/v1/payroll/runs/{run_id}/payments"):
        r = pay["pete"].get(url)
        assert r.status_code == 200, (url, r.text[:200])
        assert emma_number not in r.text and "beneficiary_enc" not in r.text, url
        assert "no-store" in r.headers.get("cache-control", ""), url


# ---------------------------------------------------------------------------
# Insufficient funding information and missing beneficiaries (Goal 2 §12, EC-PAY-03)
# ---------------------------------------------------------------------------
def test_ec_pay_03_insufficient_funding_info_or_a_missing_beneficiary_never_creates_a_payment(pay):
    """Goal 2 §12 "insufficient funding info", EC-PAY-03: Emma has an approved bank account, Ivan none. The
    proposal batches Emma's 1000.00 only and reports Ivan's 2000.00 as excluded (MISSING_BANK_ACCOUNT). Once the
    only funding account is deactivated (reason + fresh step-up), nothing can be batched: the proposal holds no
    batch and names NO_FUNDING_ACCOUNT for Emma, creation is 409 NOTHING_TO_BATCH, no batch row exists, every
    instruction stays outside a batch and both obligations stay fully outstanding (1000.00 and 2000.00)."""
    emma, ivan = pay["emp"]["Emma"]["id"], pay["emp"]["Ivan"]["id"]
    add_bank_account(pay["cid"], emma, "771600001111", holder="Emma Test")
    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"}, with_accounts=False)
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert [(b["item_count"], b["total"]) for b in proposal["batches"]] == [(1, {"amount": "1000.00",
                                                                                "currency": "INR"})], proposal
    assert [(e["employee_id"], e["reason"], e["amount"]) for e in proposal["exclusions"]] == [
        (ivan, "MISSING_BANK_ACCOUNT", {"amount": "2000.00", "currency": "INR"})], proposal["exclusions"]
    assert "771600001111" not in json.dumps(proposal)  # the beneficiary is masked in the report

    step_up(pay["fran"])
    r = pay["fran"].post(f"/api/v1/payroll/funding-accounts/{pay['funding']}/deactivate",
                         json={"reason": "Account closed by the bank"})
    assert r.status_code == 200, r.text
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert proposal["batches"] == [], proposal
    assert sorted((e["employee_id"], e["reason"]) for e in proposal["exclusions"]) == sorted(
        [(emma, "NO_FUNDING_ACCOUNT"), (ivan, "MISSING_BANK_ACCOUNT")]), proposal["exclusions"]
    r = pay["pete"].post(BATCHES, json={"run_id": run_id})
    assert r.status_code == 409 and code_of(r) == "NOTHING_TO_BATCH", r.text
    assert sql("SELECT count(*) FROM payment_batches WHERE company_id = :c", c=pay["cid"])[0][0] == 0
    assert all(row[2] is None for row in live_instructions(run_id)), live_instructions(run_id)
    outstanding = {i["employee_id"]: i["outstanding"]
                   for i in ok(pay["fran"].get(OBLIGATIONS, params={"run_id": run_id}))["items"]}
    assert outstanding == {emma: {"amount": "1000.00", "currency": "INR"},
                           ivan: {"amount": "2000.00", "currency": "INR"}}, outstanding


# ---------------------------------------------------------------------------
# Payroll switched off with payments pending (EC-RUN-18)
# ---------------------------------------------------------------------------
PRESERVED_TABLES = ("payroll_runs", "payroll_run_employees", "payroll_lines", "documents", "payment_obligations",
                    "payment_instructions", "payment_batches", "payment_files", "payment_events", "bank_accounts")


def _payroll_fingerprint(company_id):
    """{table: (rows, md5 of the rows)} of the tenant's run, document and payment tables (owner connection)."""
    from sqlalchemy import text

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return {t: tuple(conn.execute(text(
            f'SELECT count(*), coalesce(md5(string_agg(md5(x::text), \',\' ORDER BY md5(x::text))), \'\') '
            f'FROM "{t}" x WHERE company_id = :c'), {"c": company_id}).one()) for t in PRESERVED_TABLES}


def test_ec_run_18_switching_payroll_off_preserves_finalized_runs_documents_and_pending_payments(pay):
    """EC-RUN-18 (3.5 "Feature-flag rollback": turning payroll off hides the screens and never deletes or alters
    finalized runs, documents or pending payments; registry: OFF = "no runs can be created"). Regression for the
    defect found on 23 Oct 2026: payroll.enabled was enforced nowhere, so runs could still be created with payroll
    switched off. A finalized run (Emma 1000.00 + Ivan 2000.00 = 3000.00) has an approved batch with its file,
    waiting for release (run PAYMENT_READY). payroll.enabled is switched OFF at TENANT level (with a reason, the current period is
    finalized): a new run is refused (409 PAYROLL_DISABLED) and every run, document and payment row is
    byte-identical. Switched back ON, the same batch is released by the signatory and its two payments are
    SUBMITTED for exactly 1000.00 and 2000.00."""
    from tests.payroll.verification.conftest import put_setting, today_of

    admin, cid = pay["admin"], pay["cid"]
    emma, ivan = pay["emp"]["Emma"]["id"], pay["emp"]["Ivan"]["id"]
    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    off_cycle = {"run_type": "OFF_CYCLE", "pay_group_id": pay["group"], "pay_period_id": pay["period"]["id"],
                 "employee_ids": [emma]}
    before = _payroll_fingerprint(cid)
    status_before = run_status(run_id)  # PAYMENT_READY once a batch exists (the run itself stays finalized)
    assert status_before == "PAYMENT_READY", status_before

    today = today_of(admin)
    put_setting(admin, "payroll.enabled", False, effective_from=today, reason="Rollback rehearsal (EC-RUN-18)")
    r = admin.post("/api/v1/payroll/runs", json=off_cycle)
    assert r.status_code == 409 and code_of(r) == "PAYROLL_DISABLED", r.text
    detail = admin.get(f"/api/v1/payroll/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["status"] == status_before, detail.text[:200]
    after = _payroll_fingerprint(cid)
    assert after == before, [t for t in PRESERVED_TABLES if after[t] != before[t]]

    put_setting(admin, "payroll.enabled", True, effective_from=today, reason="Rollback rehearsal over (EC-RUN-18)")
    assert _payroll_fingerprint(cid) == before
    step_up(pay["fran"])
    released = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/release"))
    assert released["status"] == "SUBMITTED", released
    paid = instructions_of(run_id)
    assert {e: (v["status"], Decimal(v["amount"]), v["batch_id"]) for e, v in paid.items()} == {
        emma: ("SUBMITTED", Decimal("1000.00"), batch["id"]), ivan: ("SUBMITTED", Decimal("2000.00"), batch["id"])}
    # Back ON, run creation works again (the off-cycle run for Emma is created in DRAFT).
    r = admin.post("/api/v1/payroll/runs", json=off_cycle)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Accounting journals of a run and of its reversal (EC-PAY-22)
# ---------------------------------------------------------------------------
from tests.payroll.test_reversals import approve as approve_reversal  # noqa: E402
from tests.payroll.test_reversals import reversal_world  # noqa: E402,F401  (fixture)
from tests.payroll.test_reversals import reverse as request_reversal  # noqa: E402
from tests.payroll.test_retro import finalize as finalize_run  # noqa: E402
from tests.payroll.test_retro import retro_world, runs_world  # noqa: E402,F401  (fixtures)

# Hand-picked GL accounts per line kind (fictitious chart of accounts).
GL_BY_KIND = {"EARNING": ("6000", "2100"), "DEDUCTION": ("2100", "2200"), "EMPLOYER_CONTRIBUTION": ("6100", "2200"),
              "REIMBURSEMENT": ("6200", "2100"), "ROUNDING": ("6900", "2100")}


def _journal_entries(client, run_id):
    listed = ok(client.get("/api/v1/payroll/accounting/journals", params={"run_id": run_id}))["items"]
    assert len(listed) == 1, listed
    detail = ok(client.get(f"/api/v1/payroll/accounting/journals/{listed[0]['id']}"))
    entries = {}
    for line in detail["lines"]:
        key = (line["component_code"], line["side"], line["account"])
        entries[key] = entries.get(key, Decimal("0")) + Decimal(line["amount"]["amount"])
    return listed[0], entries


def test_ec_pay_22_a_reversal_posts_the_exact_opposite_journal_balanced_and_only_once(reversal_world):
    """EC-PAY-22 (accounting ON: journals always balance, generation is idempotent, reversals post opposite
    entries). Emma and Ivan each have an approved annual CTC of 1200000 (runs_world), so per month:
    1200000 / 12 = 100000.00; BASIC = 50% = 50000.00; HRA = 40% of BASIC = 20000.00 (EXAMPLE_A). With EARNING
    lines mapped debit 6000 / credit 2100, the original run's journal holds BASIC debit 6000 = 50000.00 x 2 =
    100000.00 and credit 2100 = 100000.00, HRA 20000.00 x 2 = 40000.00 on each side. The approved reversal of the
    whole run, once finalized, posts its own journal where every entry of the original appears on the opposite
    side of the same account for the same amount (BASIC debit 2100 = 100000.00, credit 6000 = 100000.00), both
    journals balance per currency, and the finalized event delivered again never posts a second journal."""
    from tests.payroll.test_wage_inputs import set_setting

    w = reversal_world
    acme, cid, run_id = w["acme"], w["cid"], w["run"]["id"]
    set_setting(cid, "payroll.accounting.enabled", True)
    set_setting(cid, "payroll.accounting.post_on", "FINALIZE")
    kinds = sql("SELECT DISTINCT component_code, kind FROM payroll_lines WHERE run_id = :r AND kind IN "
                "('EARNING', 'DEDUCTION', 'EMPLOYER_CONTRIBUTION', 'REIMBURSEMENT', 'ROUNDING')", r=run_id)
    assert {"BASIC", "HRA"} <= {code for code, _ in kinds}, kinds
    for code, kind in kinds:
        debit, credit = GL_BY_KIND[kind]
        ok(acme.put("/api/v1/payroll/accounting/gl-mappings", json={
            "component_code": code, "debit_account": debit, "credit_account": credit}))

    posted = ok(acme.post(f"/api/v1/payroll/runs/{run_id}/journal"))
    assert posted["status"] == "POSTED", posted
    original, original_entries = _journal_entries(acme, run_id)
    assert original_entries[("BASIC", "DEBIT", "6000")] == Decimal("100000.00")
    assert original_entries[("BASIC", "CREDIT", "2100")] == Decimal("100000.00")
    assert original_entries[("HRA", "DEBIT", "6000")] == Decimal("40000.00")
    assert original_entries[("HRA", "CREDIT", "2100")] == Decimal("40000.00")
    for totals in original["totals"].values():
        assert totals["debit"] == totals["credit"], original["totals"]

    created = request_reversal(acme, run_id)
    approve_reversal(w["approver"], created["id"])
    reversal_run = approve_reversal(w["second"], created["id"])["reversal_run_id"]
    finalize_run(w, reversal_run)  # emits payroll.run.finalized: the accounting subscriber posts the journal
    reversal, reversal_entries = _journal_entries(acme, reversal_run)
    assert reversal["status"] == "POSTED" and reversal["trigger"] == "FINALIZE", reversal
    assert reversal_entries[("BASIC", "DEBIT", "2100")] == Decimal("100000.00")
    assert reversal_entries[("BASIC", "CREDIT", "6000")] == Decimal("100000.00")
    assert reversal_entries[("HRA", "DEBIT", "2100")] == Decimal("40000.00")
    assert reversal_entries[("HRA", "CREDIT", "6000")] == Decimal("40000.00")
    opposite = {(code, "CREDIT" if side == "DEBIT" else "DEBIT", account): amount
                for (code, side, account), amount in original_entries.items()}
    assert reversal_entries == opposite
    assert {c: (t["debit"], t["credit"]) for c, t in reversal["totals"].items()} == {
        c: (t["credit"], t["debit"]) for c, t in original["totals"].items()}
    for totals in reversal["totals"].values():
        assert totals["debit"] == totals["credit"], reversal["totals"]

    # Idempotent: the finalized event again, and a manual post, return the same journal.
    from app.payroll.services import outbox
    from tests.payroll.test_wage_inputs import session

    with session(cid) as (db, _):
        outbox.emit(db, "payroll.run.finalized", {"run_id": reversal_run})
        db.commit()
    again = ok(acme.post(f"/api/v1/payroll/runs/{reversal_run}/journal"))
    assert again["id"] == reversal["id"]
    assert sql("SELECT count(*) FROM accounting_journals WHERE company_id = :c AND run_id IN (:a, :b)",
               c=cid, a=run_id, b=reversal_run)[0][0] == 2
