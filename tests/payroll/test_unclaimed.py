"""Unclaimed pay as company policy (EC-PAY-19): the setting ``payroll.payments.unclaimed_after_days`` (default off),
the scan that moves unresolved obligations to UNCLAIMED, the release with a reason and step-up, the audit trail, the
notifications, the per-currency totals and tenant isolation.

It is a company policy value, not law: escheatment is country-specific and not implemented. Nothing in these tests
pays, deletes or reverses money: marking holds unsent instructions, releasing lets the system re-check readiness.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.payroll.services import unclaimed
from tests.payroll.test_payment_obligations import (  # noqa: F401  (fixtures and helpers)
    OBLIGATIONS,
    BATCHES,
    err,
    finalized_run,
    instructions_of,
    ok,
    pay,
    payments_world,
    runs_world,
    set_entity_setting,
    step_up,
    submitted_batch,
)
from tests.payroll.test_wage_inputs import CHECK, DENIED, audit_actions, expect_error, session, sql, tenant

BASE = "/api/v1/payroll/unclaimed-payments"
KEY = "payroll.payments.unclaimed_after_days"
INSTRUCTIONS = "/api/v1/payroll/payment-instructions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def age(obligation_id, days):
    """The obligation's pay date `days` days before today in its legal entity's time zone (the scan's own clock:
    the database date can differ from it for some hours of the day)."""
    from app.models.org import LegalEntity
    from app.payroll.services import self_service

    cid, entity_id = sql("SELECT company_id, legal_entity_id FROM payment_obligations WHERE id = :i", i=obligation_id)[0]
    with session(cid) as (db, _):
        today = self_service.local_today(db, cid, db.query(LegalEntity).filter(LegalEntity.id == entity_id).one())
    sql("UPDATE payment_obligations SET pay_date = :d WHERE id = :i", d=today - timedelta(days=days), i=obligation_id)


def obligation_of(world, run_id, name):
    return sql("SELECT id, status, row_version FROM payment_obligations WHERE run_id = :r AND employee_id = :e",
               r=run_id, e=world["emp"][name]["id"])[0]


def scan_api(client, **body):
    r = client.post(f"{BASE}/scan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def release(client, obligation_id, reason="Employee confirmed a new account", expect=200, **body):
    r = client.post(f"{BASE}/{obligation_id}/release", json={"reason": reason, **body})
    assert r.status_code == expect, r.text
    return r.json()


def events_of(obligation_id):
    return sql("SELECT action, window_days, result_status, actor_user_id, held_instruction_ids::text, reason "
               "FROM payment_unclaimed_events WHERE obligation_id = :i ORDER BY id", i=obligation_id)


def notifications_of(client):
    return [t for (t,) in sql("SELECT title FROM notifications WHERE user_id = :u ORDER BY id", u=client.user_id)]


def prepare(world, days=30):
    set_entity_setting(world, KEY, days)
    return finalized_run(world, {"Emma": "50000.00", "Ivan": "42000.50"})


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------
def test_the_setting_is_off_by_default_bounded_and_labelled_company_policy():
    from app.payroll import settings_registry as reg

    definition = reg.REGISTRY[KEY]
    assert definition.default is None and definition.nullable and definition.type == "int"
    assert definition.constraints["min"] == 1 and definition.legal_sensitivity == "NONE"
    assert "company policy" in definition.help.lower() and "not implemented" in definition.help.lower()
    assert reg.validate_value(KEY, None) is None and reg.validate_value(KEY, 30) == 30
    for bad in (0, -5, 3651, "30", 1.5, True):
        with pytest.raises(reg.SettingValidationError):
            reg.validate_value(KEY, bad)


@pytest.mark.parametrize("statuses, eligible, reason", [
    (["READY"], True, "UNRESOLVED"),
    (["NOT_READY"], True, "UNRESOLVED"),
    (["FAILED"], True, "UNRESOLVED"),
    (["REJECTED", "RETURNED"], True, "UNRESOLVED"),
    (["REVERSED", "REISSUED"], True, "UNRESOLVED"),
    (["SETTLED", "READY"], True, "UNRESOLVED"),                  # a part is paid, the rest never left
    (["ON_HOLD"], False, "ON_HOLD"),
    (["READY", "ON_HOLD"], False, "ON_HOLD"),
    (["APPROVAL_PENDING"], False, "PAYMENT_IN_PROGRESS"),
    (["APPROVED", "FAILED"], False, "PAYMENT_IN_PROGRESS"),
    (["SUBMITTED"], False, "PAYMENT_IN_PROGRESS"),
    (["PARTIALLY_SETTLED"], False, "PAYMENT_IN_PROGRESS"),
    (["SETTLED"], False, "NOTHING_UNRESOLVED"),
    (["CANCELLED"], False, "NOTHING_UNRESOLVED"),
    ([], False, "NOTHING_UNRESOLVED"),
    (["MYSTERY"], False, "UNKNOWN_STATUS"),
])
def test_ec_pay_19_what_may_be_marked(statuses, eligible, reason):
    assert unclaimed.classify(statuses) == (eligible, reason)


def test_the_window_counts_from_the_pay_date_and_restarts_at_a_release():
    pay_date, created = date(2026, 9, 1), date(2026, 9, 5)
    assert unclaimed.due_on(pay_date, None, created, 30) == date(2026, 10, 1)
    assert unclaimed.due_on(None, None, created, 30) == date(2026, 10, 5)
    assert unclaimed.due_on(pay_date, date(2026, 9, 20), created, 30) == date(2026, 10, 20)
    assert unclaimed.due_on(pay_date, date(2026, 8, 1), created, 30) == date(2026, 10, 1)   # an old release counts for nothing


def test_the_next_scan_is_the_next_two_in_the_morning_utc():
    from datetime import datetime, timezone

    assert unclaimed.next_scan_at(datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
    assert unclaimed.next_scan_at(datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def test_ec_pay_19_nothing_moves_while_the_policy_is_off(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00"})
    obligation_id = obligation_of(pay, run_id, "Emma")[0]
    age(obligation_id, 4000)
    outcome = scan_api(pay["alma"])
    assert outcome["marked"] == [] and outcome["entities_off"] == [pay["le"]]
    assert obligation_of(pay, run_id, "Emma")[1] == "OPEN"
    assert events_of(obligation_id) == []
    assert sql("SELECT count(*) FROM payment_unclaimed_events")[0][0] == 0


def test_ec_pay_19_an_unpaid_obligation_older_than_the_window_becomes_unclaimed_and_unsent_payments_are_held(pay):
    run_id = prepare(pay)
    emma_id, ivan_id = obligation_of(pay, run_id, "Emma")[0], obligation_of(pay, run_id, "Ivan")[0]
    age(emma_id, 31)
    age(ivan_id, 29)
    outcome = scan_api(pay["alma"])
    assert [m["obligation_id"] for m in outcome["marked"]] == [emma_id]
    assert outcome["marked"][0]["amount"] == "50000.00" and outcome["marked"][0]["currency"] == "INR"
    assert obligation_of(pay, run_id, "Emma")[1] == "UNCLAIMED" and obligation_of(pay, run_id, "Ivan")[1] == "OPEN"
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]
    assert (emma["status"], emma["reason"]) == ("ON_HOLD", "UNCLAIMED")
    assert instructions_of(run_id)[pay["emp"]["Ivan"]["id"]]["status"] == "READY"
    # The log, the audit trail and the in-app notices; no amount is in a notification.
    [(action, window, result, actor, held, reason)] = events_of(emma_id)
    assert (action, window, result) == ("MARKED", 30, "UNCLAIMED") and actor == pay["alma"].user_id
    assert str(emma["id"]) in held and "company policy" in reason and "not implemented" in reason
    assert "UNCLAIMED_MARKED" in audit_actions(pay["cid"], "payment_obligation", emma_id)
    assert "Unclaimed pay" in notifications_of(pay["alma"]) and "Unclaimed pay" in notifications_of(pay["fran"])
    assert not [t for t in notifications_of(pay["fran"]) if "50000" in t]
    # No money moved: no payment event of a status the scan would not own, nothing batched.
    assert sql("SELECT count(*) FROM payment_instructions WHERE obligation_id = :o AND batch_id IS NOT NULL", o=emma_id)[0][0] == 0
    # A second scan does nothing more (idempotent), and the obligation shows up in the unclaimed list.
    again = scan_api(pay["alma"])
    assert again["marked"] == []
    listed = ok(pay["pete"].get(BASE))
    assert [i["id"] for i in listed["items"]] == [emma_id] and listed["items"][0]["held_instructions"] == 1
    assert listed["totals"] == [{"currency": "INR", "count": 1, "outstanding": {"amount": "50000.00", "currency": "INR"}}]
    assert listed["policy"]["note"].startswith("company policy") and listed["items"][0]["window_days"] == 30
    assert ok(pay["pete"].get(f"{BASE}/{emma_id}/history"))["items"][0]["action"] == "MARKED"


def test_ec_pay_19_payments_in_progress_and_deliberate_holds_are_never_overridden(pay):
    set_entity_setting(pay, KEY, 30)
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50", "Zara": "10000.00", "Nell": "9000.00"},
                           held=("Nell",))
    ids = {n: obligation_of(pay, run_id, n)[0] for n in ("Emma", "Ivan", "Zara", "Nell")}
    for obligation_id in ids.values():
        age(obligation_id, 90)
    # Zara's payment is batched and submitted (in flight); Nell's employee payment hold is a person's decision.
    sql("UPDATE payment_instructions SET status = 'SUBMITTED', submitted_at = now(), submission_channel = 'MANUAL_UPLOAD' "
        "WHERE obligation_id = :o", o=ids["Zara"])
    assert instructions_of(run_id)[pay["emp"]["Nell"]["id"]]["status"] == "ON_HOLD"
    outcome = scan_api(pay["alma"])
    reasons = {s["obligation_id"]: s["reason"] for s in outcome["skipped"]}
    assert reasons == {ids["Zara"]: "PAYMENT_IN_PROGRESS", ids["Nell"]: "ON_HOLD"}
    assert sorted(m["obligation_id"] for m in outcome["marked"]) == sorted([ids["Emma"], ids["Ivan"]])
    assert obligation_of(pay, run_id, "Zara")[1] == "OPEN" and obligation_of(pay, run_id, "Nell")[1] == "OPEN"
    # The held employee payment keeps its own reason.
    assert instructions_of(run_id)[pay["emp"]["Nell"]["id"]]["reason"] == "EMPLOYEE_HOLD"


def test_ec_pay_19_a_failed_payment_without_a_reissue_is_unclaimed_but_cannot_be_reissued_or_released_alone(pay):
    run_id = prepare(pay)
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    instruction = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    sql("UPDATE payment_instructions SET status = 'FAILED', failure_code = 'AC04', failure_message = 'Account closed' "
        "WHERE id = :i", i=instruction)
    age(emma_id, 60)
    assert [m["obligation_id"] for m in scan_api(pay["alma"])["marked"]] == [emma_id]
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "FAILED"          # a failure stays visible
    err(pay["pete"].post(f"{INSTRUCTIONS}/{instruction}/reissue", json={"reason": "Account fixed"}), 409,
        "OBLIGATION_UNCLAIMED")
    # A hold placed by the policy is released with the obligation, not instruction by instruction.
    sql("UPDATE payment_instructions SET status = 'ON_HOLD', status_reason = 'UNCLAIMED' WHERE id = :i", i=instruction)
    step_up(pay["alma"])
    err(pay["alma"].post(f"{INSTRUCTIONS}/{instruction}/release-hold", json={"reason": "Let it go"}), 409,
        "OBLIGATION_UNCLAIMED")


def test_ec_pay_19_the_scan_respects_each_legal_entity_and_an_invalid_value_fails_closed(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00"})
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 500)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, 'LEGAL_ENTITY', :le, '\"soon\"'::jsonb, false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=pay["cid"], k=KEY, le=pay["le"])
    outcome = scan_api(pay["alma"])
    assert outcome["marked"] == [] and outcome["errors"][0]["code"] == "CONFIG_INVALID"
    assert obligation_of(pay, run_id, "Emma")[1] == "OPEN"
    # A scan of an entity outside the caller's scope is 404, and another company's entity id never resolves.
    assert pay["alma"].post(f"{BASE}/scan", json={"legal_entity_id": 999999}).status_code == 404


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------
def test_ec_pay_19_release_needs_step_up_a_reason_and_the_window_restarts(pay):
    run_id = prepare(pay)
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 45)
    scan_api(pay["alma"])
    version = obligation_of(pay, run_id, "Emma")[2]
    r = pay["alma"].post(f"{BASE}/{emma_id}/release", json={"reason": "Employee confirmed a new account"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    step_up(pay["alma"])
    assert pay["alma"].post(f"{BASE}/{emma_id}/release", json={"reason": "ok"}).status_code == 422
    assert pay["alma"].post(f"{BASE}/{emma_id}/release", json={"reason": ""}).status_code == 422
    assert pay["alma"].post(f"{BASE}/{emma_id}/release", json={"reason": "Account fixed", "status": "SETTLED"}).status_code == 422
    release(pay["alma"], emma_id, row_version=version + 5, expect=409)
    done = release(pay["alma"], emma_id, row_version=version)
    assert done["status"] == "OPEN" and done["row_version"] > version
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "READY"          # re-checked, usable again
    assert [e[0] for e in events_of(emma_id)] == ["MARKED", "RELEASED"]
    released = events_of(emma_id)[1]
    assert released[2] == "OPEN" and released[3] == pay["alma"].user_id and "new account" in released[5]
    assert "UNCLAIMED_RELEASED" in audit_actions(pay["cid"], "payment_obligation", emma_id)
    # Not marked again at once: the window restarts from the release day (the pay date is still 45 days old).
    assert scan_api(pay["alma"])["marked"] == []
    assert obligation_of(pay, run_id, "Emma")[1] == "OPEN"
    assert ok(pay["alma"].get(BASE))["items"] == []
    # Only an unclaimed obligation can be released.
    step_up(pay["alma"])
    release(pay["alma"], emma_id, expect=409)
    assert pay["alma"].post(f"{BASE}/{emma_id}/release", json={"reason": "again please"}).json()["code"] == "NOT_UNCLAIMED"


def test_ec_pay_19_release_goes_back_to_the_system_checks_not_blindly_to_ready(pay):
    run_id = prepare(pay)
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 45)
    scan_api(pay["alma"])
    # The account stopped being usable while the pay was unclaimed: the released payment waits (NOT_READY),
    # it is not pushed to READY.
    sql("UPDATE bank_accounts SET status = 'INACTIVE', ended_at = now() WHERE employee_id = :e",
        e=pay["emp"]["Emma"]["id"])
    step_up(pay["alma"])
    assert release(pay["alma"], emma_id)["status"] == "OPEN"
    emma = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]
    assert emma["status"] in ("NOT_READY", "ON_HOLD") and emma["status"] != "READY"


def test_ec_pay_19_permissions_scope_and_tenants(pay, app):
    from tests.payroll.test_compensation import login_as
    from tests.helpers import create_employee, signup_company

    run_id = prepare(pay)
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 45)
    scan_api(pay["alma"])
    # Without a payment permission nothing is readable or doable (the founder runs payroll, not payments).
    for call in (lambda c: c.get(BASE), lambda c: c.get(f"{BASE}/{emma_id}/history"),
                 lambda c: c.post(f"{BASE}/scan", json={}), lambda c: c.post(f"{BASE}/{emma_id}/release", json={"reason": "abc"})):
        assert call(pay["admin"]).status_code == 403
    # Other tenants see and reach nothing of it.
    other = signup_company(app, "Other Co")
    outsider = login_as(app, other, create_employee(other, "Olga"), ["finance_payments"])
    step_up(outsider)
    assert outsider.get(BASE).json()["items"] == [] and outsider.get(BASE).json()["totals"] == []
    assert outsider.get(f"{BASE}/{emma_id}/history").status_code == 404
    assert outsider.post(f"{BASE}/{emma_id}/release", json={"reason": "abc"}).status_code == 404
    assert outsider.post(f"{BASE}/scan", json={}).json()["marked"] == []
    assert obligation_of(pay, run_id, "Emma")[1] == "UNCLAIMED"
    for cid in (other.company_id, None):
        with tenant(cid) as conn:
            assert conn.exec_driver_sql("SELECT count(*) FROM payment_unclaimed_events").scalar() == 0
    with tenant(pay["cid"]) as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM payment_unclaimed_events").scalar() == 1


def test_the_unclaimed_log_is_append_only_and_keeps_its_own_rules(pay):
    run_id = prepare(pay)
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 45)
    scan_api(pay["alma"])
    entry = sql("SELECT id FROM payment_unclaimed_events WHERE obligation_id = :i", i=emma_id)[0][0]
    cid, emma_employee = pay["cid"], pay["emp"]["Emma"]["id"]
    insert = ("INSERT INTO payment_unclaimed_events (company_id, obligation_id, employee_id, legal_entity_id, action, reason, "
              "window_days, amount, currency, held_instruction_ids, actor_user_id, at, created_at, updated_at) VALUES "
              "(:c, :o, :e, :le, :a, :r, :w, :m, :cur, '[]'::jsonb, :u, now(), now(), now())")
    base = dict(c=cid, o=emma_id, e=emma_employee, le=pay["le"], a="MARKED", r="because", w=30, m=1, cur="INR", u=None)
    with tenant(cid) as conn:
        expect_error(conn, CHECK, "UPDATE payment_unclaimed_events SET reason = 'edited' WHERE id = :i", i=entry)
        expect_error(conn, CHECK, "DELETE FROM payment_unclaimed_events WHERE id = :i", i=entry)
        expect_error(conn, CHECK, insert, **{**base, "a": "RELEASED"})                   # a release names who released
        expect_error(conn, CHECK, insert, **{**base, "w": None})                          # a marking names its cause
        expect_error(conn, CHECK, insert, **{**base, "w": 0})
        expect_error(conn, CHECK, insert, **{**base, "r": "  "})
        expect_error(conn, CHECK, insert, **{**base, "m": 0})
        expect_error(conn, CHECK, insert, **{**base, "cur": "inr"})
        expect_error(conn, "23503", insert, **{**base, "o": 999999})                      # composite same-company key
        expect_error(conn, CHECK, insert, **{**base, "a": "DELETED"})


# ---------------------------------------------------------------------------
# Totals per currency and the jobs
# ---------------------------------------------------------------------------
def test_ec_pay_19_totals_are_per_currency(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    emma_id, ivan_id = obligation_of(pay, run_id, "Emma")[0], obligation_of(pay, run_id, "Ivan")[0]
    # A second-currency obligation (an employee paid in AED) that is unclaimed too.
    sql("INSERT INTO payment_obligations (company_id, source_type, run_id, run_employee_id, payee_type, employee_id, "
        "legal_entity_id, pay_group_id, amount, currency, status, pay_date, row_version, created_at, updated_at) "
        "SELECT company_id, source_type, run_id, run_employee_id, payee_type, employee_id, legal_entity_id, pay_group_id, "
        "1234.5, 'AED', 'UNCLAIMED', pay_date, 1, now(), now() FROM payment_obligations WHERE id = :i", i=emma_id)
    sql("UPDATE payment_obligations SET status = 'UNCLAIMED' WHERE id = ANY(:ids)", ids=[emma_id, ivan_id])
    listed = ok(pay["pete"].get(BASE))
    assert listed["total"] == 3
    assert listed["totals"] == [
        {"currency": "AED", "count": 1, "outstanding": {"amount": "1234.50", "currency": "AED"}},
        {"currency": "INR", "count": 2, "outstanding": {"amount": "92000.50", "currency": "INR"}},
    ]
    only = ok(pay["pete"].get(BASE, params={"currency": "AED"}))
    assert only["total"] == 1 and only["totals"][0]["currency"] == "AED"
    assert ok(pay["pete"].get(BASE, params={"legal_entity_id": pay["le"] + 1}))["total"] == 0
    # The obligations report and list already carry UNCLAIMED as a status (EC-PAY-19).
    assert {o["status"] for o in ok(pay["pete"].get(OBLIGATIONS, params={"status": "UNCLAIMED"}))["items"]} == {"UNCLAIMED"}


def test_ec_pay_19_a_finalized_run_seeds_the_daily_scan_and_the_job_marks_and_requeues(pay):
    from app.payroll.services import jobs

    run_id = prepare(pay)
    queued = sql("SELECT count(*), min(run_at) FROM jobs WHERE company_id = :c AND type = 'payments.unclaimed_scan' "
                 "AND status = 'QUEUED'", c=pay["cid"])[0]
    assert queued[0] == 1 and queued[1] is not None                       # seeded by the finalization event, once
    emma_id = obligation_of(pay, run_id, "Emma")[0]
    age(emma_id, 45)
    with session(pay["cid"]) as (db, _):
        jobs.enqueue(db, company_id=pay["cid"], type=unclaimed.JOB, payload={"slot": "test"},
                     idempotency_key="unclaimed-scan:test")
        db.commit()
    assert obligation_of(pay, run_id, "Emma")[1] == "UNCLAIMED"           # inline job ran after the commit
    assert events_of(emma_id)[0][3] is None                                # the job has no acting user
    assert sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type = 'payments.unclaimed_scan' AND status = 'QUEUED'",
               c=pay["cid"])[0][0] == 1                                   # the next day's scan, one per day
    assert [a for a in audit_actions(pay["cid"], "payment_obligation", emma_id)] == ["UNCLAIMED_MARKED"]
