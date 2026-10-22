"""Inbound payment webhooks: POST /api/v1/webhooks/payments/{adapter} (3.7 §7.2, §12; EC-PAY-02, EC-PAY-06,
EC-PAY-07, EC-PAY-08; F-10). No session: signature + timestamp window + replay protection; the tenant comes
from the provider reference; SANDBOX events never settle production instructions."""

import json
import time

from tests.helpers import new_client
from tests.payroll.test_payment_events import instruction, sandbox_world
from tests.payroll.test_payment_obligations import (
    finalized_run,
    instructions_of,
    ok,
    pay,  # noqa: F401  (fixture)
    run_status,
    submitted_batch,
)
from tests.payroll.test_wage_inputs import sql

URL = "/api/v1/webhooks/payments/SANDBOX"


def provider_ref(instruction_id):
    return sql("SELECT provider_ref FROM payment_instructions WHERE id = :i", i=instruction_id)[0][0]


def deliver(app, payload, *, timestamp=None, secret=None, signature=None, url=URL):
    from app.payroll.services import payment_adapters as adapters

    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    ts = int(time.time()) if timestamp is None else timestamp
    header = signature if signature is not None else adapters.sign(body, ts, secret)
    client = new_client(app)  # no cookies, no session
    return client.http.post(url, content=body, headers={adapters.SIGNATURE_HEADER: header,
                                                        "Content-Type": "application/json"})


def inbox():
    return sql("SELECT adapter, event_id, claimed_event_id, outcome, error_code FROM payment_webhook_events "
               "ORDER BY id")


def test_webhook_inbox_holds_no_tenant_data_and_is_closed_to_the_app_role(app):
    """The platform inbox stores no company or instruction id (the applied event is the tenant's
    payment_events row under row-level security), so it is not a tenant table (platform contract)."""
    columns = {r[0] for r in sql("SELECT column_name FROM information_schema.columns "
                                 "WHERE table_name = 'payment_webhook_events'")}
    assert columns and not columns & {"company_id", "instruction_id", "employee_id"}


def test_signed_sandbox_events_acknowledge_then_settle_with_verified_evidence(app, pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    ref = provider_ref(iid)
    r = deliver(app, {"id": "evt_ack_0001", "provider_ref": ref, "status": "ACKNOWLEDGED"})
    assert r.status_code == 200 and r.json() == {"outcome": "APPLIED"}, r.text
    assert (instruction(iid)["status"], instruction(iid)["evidence_status"]) == ("ACKNOWLEDGED", "UNVERIFIED")  # EC-PAY-02
    r = deliver(app, {"id": "evt_set_0001", "provider_ref": ref, "status": "SETTLED", "amount": "100.00",
                      "currency": "INR"})
    assert r.json() == {"outcome": "APPLIED"}
    row = instruction(iid)
    assert (row["status"], row["evidence_status"], row["evidence_source"]) == ("SETTLED", "VERIFIED", "WEBHOOK")
    assert run_status(run_id) == "PAID"
    events = sql("SELECT source, sandbox, external_event_id FROM payment_events WHERE instruction_id = :i AND "
                 "source = 'WEBHOOK' ORDER BY id", i=iid)
    assert [tuple(e) for e in events] == [("WEBHOOK", True, "evt_ack_0001"), ("WEBHOOK", True, "evt_set_0001")]
    assert [(r[1], r[3]) for r in inbox()] == [("evt_ack_0001", "PROCESSED"), ("evt_set_0001", "PROCESSED")]
    # The tenant-side record of each applied event is its payment_events row (same external id).
    assert sql("SELECT count(*) FROM payment_events WHERE company_id = :c AND external_event_id IN "
               "('evt_ack_0001', 'evt_set_0001')", c=pay["cid"])[0][0] == 2


def test_ec_pay_06_duplicate_delivery_is_processed_once(app, pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    payload = {"id": "evt_dup_0001", "provider_ref": provider_ref(iid), "status": "SETTLED"}
    assert deliver(app, payload).json() == {"outcome": "APPLIED"}
    second = deliver(app, payload)
    assert second.status_code == 200 and second.json() == {"outcome": "DUPLICATE"}
    assert sql("SELECT count(*) FROM payment_events WHERE external_event_id = 'evt_dup_0001'")[0][0] == 1
    assert sql("SELECT count(*) FROM payment_webhook_events WHERE event_id = 'evt_dup_0001'")[0][0] == 1


def test_ec_pay_07_out_of_order_webhooks_never_move_backwards(app, pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    ref = provider_ref(iid)
    assert deliver(app, {"id": "evt_ooo_0002", "provider_ref": ref, "status": "SETTLED"}).json()["outcome"] == "APPLIED"
    late = deliver(app, {"id": "evt_ooo_0001", "provider_ref": ref, "status": "ACKNOWLEDGED"})
    assert late.json() == {"outcome": "STALE"}
    assert instruction(iid)["status"] == "SETTLED"


def test_ec_pay_08_forged_stale_or_malformed_webhooks_are_rejected_and_logged(app, pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    payload = {"id": "evt_forged_01", "provider_ref": provider_ref(iid), "status": "SETTLED"}
    forged = deliver(app, payload, secret=b"x" * 32)
    assert forged.status_code == 401 and forged.json()["code"] == "SIGNATURE_INVALID"
    unsigned = deliver(app, payload, signature="")
    assert unsigned.status_code == 401 and unsigned.json()["code"] == "SIGNATURE_MISSING"
    stale = deliver(app, payload, timestamp=int(time.time()) - 3600)  # a captured, replayed delivery
    assert stale.status_code == 401 and stale.json()["code"] == "TIMESTAMP_OUT_OF_WINDOW"
    numbers = deliver(app, b'{"id": "evt_float_01", "provider_ref": "%s", "status": "SETTLED", "amount": 100.0, '
                           b'"currency": "INR"}' % provider_ref(iid).encode())
    assert numbers.status_code == 400 and numbers.json()["code"] == "PAYLOAD_INVALID"
    assert instruction(iid)["status"] == "SUBMITTED"
    rows = inbox()
    assert [(r[3], r[4]) for r in rows] == [("REJECTED", "SIGNATURE_INVALID"), ("REJECTED", "SIGNATURE_MISSING"),
                                            ("REJECTED", "TIMESTAMP_OUT_OF_WINDOW"), ("REJECTED", "PAYLOAD_INVALID")]
    assert all(r[1] is None for r in rows) and rows[0][2] == "evt_forged_01"
    # A rejected id never blocks the genuine event.
    assert deliver(app, payload).json() == {"outcome": "APPLIED"}


def test_unknown_reference_and_unknown_adapter_reveal_nothing(app, pay):
    unresolved = deliver(app, {"id": "evt_unknown_1", "provider_ref": "SBX-" + "0" * 32, "status": "SETTLED"})
    assert unresolved.status_code == 202 and unresolved.json() == {"outcome": "UNRESOLVED"}
    assert inbox()[0][3] == "UNRESOLVED"
    other = deliver(app, {"id": "evt_unknown_2", "provider_ref": "SBX-" + "0" * 32, "status": "SETTLED"},
                    url="/api/v1/webhooks/payments/STRIPE")
    assert other.status_code == 404


def test_sandbox_events_never_settle_production_instructions(app, pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)  # a real bank file marked as submitted (MANUAL_UPLOAD)
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    assert provider_ref(iid) is None  # not reachable by reference at all
    # Even with a reference planted on it, a sandbox event is a conflict for review, never a settlement.
    sql("UPDATE payment_instructions SET provider_ref = :r WHERE id = :i", r="SBX-" + "a" * 32, i=iid)
    result = deliver(app, {"id": "evt_prod_0001", "provider_ref": "SBX-" + "a" * 32, "status": "SETTLED"})
    assert result.status_code == 200 and result.json() == {"outcome": "CONFLICT"}
    row = instruction(iid)
    assert (row["status"], row["evidence_status"]) == ("SUBMITTED", "UNVERIFIED")
    detail = sql("SELECT outcome, details->>'reason' FROM payment_events WHERE external_event_id = 'evt_prod_0001'")
    assert tuple(detail[0]) == ("CONFLICT", "SANDBOX_EVENT_FOR_PRODUCTION")
    assert ok  # shared helper imported for parity


def test_webhook_currency_mismatch_is_a_conflict(app, pay):
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    result = deliver(app, {"id": "evt_ccy_0001", "provider_ref": provider_ref(iid), "status": "SETTLED",
                           "amount": "100.00", "currency": "USD"})
    assert result.json() == {"outcome": "CONFLICT"} and instruction(iid)["status"] == "SUBMITTED"
