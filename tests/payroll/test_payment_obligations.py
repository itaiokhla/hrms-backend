"""Payment obligations and instructions from finalized runs (3.7 §3.4, §8.1, §11; 3.5 §5.8; EC-PAY-15,
EC-PAY-16, EC-PAY-23, EC-BNK-03, EC-RUN-13).

The helpers and the ``pay`` fixture here (``payments_world``, ``finalized_run``, ``add_bank_account``,
``add_funding_account``, ``step_up`` ...) are shared with the other payment test modules. Runs are
created through the API and moved to FINALIZED directly (the calculation is another milestone's
concern); the outbox event ``payroll.run.finalized`` then creates the obligations exactly as in
production (JOBS_MODE=inline).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import login_as
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    calendar_for,
    current_period,
    force_status,
    new_run,
    runs_world,
)
from tests.payroll.test_wage_inputs import audit_actions, pay_group, place, session, set_setting, sql  # noqa: F401

PAY = "/api/v1/payroll"
BATCHES = f"{PAY}/payment-batches"
OBLIGATIONS = f"{PAY}/payment-obligations"
INSTRUCTIONS = f"{PAY}/payment-instructions"
BALANCES = f"{PAY}/payment-balances"
MY_PAYMENTS = "/api/v1/me/payroll/payments"
NUMBERS = {"Emma": "501001234567", "Ivan": "501009876543", "Zara": "501005550001", "Nell": "501005550002",
           "Hugo": "501005550003", "Owen": "501005550004"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def err(response, status, code=None):
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json().get("code") == code, response.text
    return response.json()


def add_bank_account(cid, employee_id, number, *, ifsc="HDFC0001234", verified=True, usable_hours_ago=72,
                     holder="Account Holder", allocation=None, replaces=None, currency="INR"):
    """An ACTIVE account approved and past cooling-off (or still cooling off with a negative age)."""
    from app.models.payments import BankAccount
    from app.payroll.services import bank_accounts as ba

    with session(cid) as (db, _):
        checked = ba.check_details(ba.BankDetails(holder_name=holder, country="IN", currency=currency,
                                                  account_number=number, routing={"ifsc": ifsc},
                                                  account_type="SAVINGS", bank_name="HDFC Bank"))
        rid = ba._next_id(db, "bank_accounts")
        sealed = ba.seal(db, cid, "bank_accounts", rid, f"payee=EMPLOYEE:{employee_id}", checked)
        now = datetime.now(timezone.utc)
        db.add(BankAccount(
            id=rid, company_id=cid, payee_type="EMPLOYEE", payee_id=employee_id, employee_id=employee_id,
            holder_name=checked.account.holder_name, country="IN", currency=currency,
            routing=dict(checked.account.routing), bank_name="HDFC Bank", account_type=checked.account.account_type,
            allocation=allocation or {"type": "PRIMARY", "value": None, "order": 0}, status="ACTIVE",
            verification_status="VERIFIED" if verified else "PENDING",
            verification_method="DOCUMENT_REVIEW" if verified else None, verified_at=now if verified else None,
            approved_at=now - timedelta(hours=max(usable_hours_ago, 0) + 1),
            usable_from=now - timedelta(hours=usable_hours_ago), effective_from=date(2024, 1, 1), risk_flags=[],
            payment_hold=False, replaces_account_id=replaces, row_version=1, **sealed))
        db.commit()
    return rid


def end_bank_account(account_id, replaced_by=None):
    sql("UPDATE bank_accounts SET status = 'INACTIVE', ended_at = now(), replaced_by_account_id = :n, "
        "row_version = row_version + 1 WHERE id = :a", a=account_id, n=replaced_by)


def add_funding_account(cid, entity_id, signatories, *, number="912020012345678", provider_profile=None,
                        formats=None, name="Payroll account", currency="INR"):
    from app.models.payments import FundingAccount
    from app.payroll.services import funding_accounts as fa

    with session(cid) as (db, _):
        row = FundingAccount(id=fa._next_id(db, fa.TABLE), company_id=cid, legal_entity_id=entity_id, name=name,
                             purpose="PAYROLL", signatory_user_ids=list(signatories), formats=list(formats or []),
                             provider_profile=provider_profile, status="ACTIVE", row_version=1)
        fa._apply_details(db, row, fa.BankDetails(holder_name="Acme Payroll", country="IN", currency=currency,
                                                  account_number=number, routing={"ifsc": "ICIC0000001"},
                                                  account_type="CURRENT", bank_name="ICICI Bank"))
        db.add(row)
        db.commit()
        return row.id


def set_entity_setting(world, key, value):
    """A legal-entity value of a setting (keys such as payroll.payments.batch_max_items are LE-level only)."""
    import json

    sql("DELETE FROM setting_values WHERE company_id = :c AND key = :k AND scope_type = 'LEGAL_ENTITY'",
        c=world["cid"], k=key)
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, :k, 'LEGAL_ENTITY', :le, CAST(:v AS jsonb), false, "
        "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=world["cid"], k=key, le=world["le"], v=json.dumps(value))


def step_up(client):
    """A fresh step-up of the client's current session (as the step-up endpoints would record it)."""
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' WHERE user_id = :u",
        u=client.user_id)


def payments_world(app, name="Acme Pay", employees=("Emma", "Ivan", "Zara", "Nell")):
    """A tenant with LE01, pay group PG-1 (INR, monthly calendar), employees placed in it, three finance users
    (Pete prepares, Alma approves batches, Fran releases them and is the funding signatory: separation of
    duties keeps the approver from releasing) and a funding account."""
    admin = signup_company(app, name)
    cid = admin.company_id
    le = sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=cid)[0][0]
    group = pay_group(cid, le, "PG-1")
    emp = {n: create_employee(admin, n, hire_date="2024-01-15") for n in employees}
    for e in emp.values():
        place(e["id"], le, group)
    calendar_for(admin, group)
    period = current_period(admin, group)
    pete = login_as(app, admin, create_employee(admin, "Pete", "Preparer"), ["finance_payments"])
    fran = login_as(app, admin, create_employee(admin, "Fran", "Finance"), ["finance_payments"])
    alma = login_as(app, admin, create_employee(admin, "Alma", "Approver"), ["finance_payments"])
    funding = add_funding_account(cid, le, [fran.user_id])
    return {"admin": admin, "cid": cid, "le": le, "group": group, "emp": emp, "period": period, "pete": pete,
            "fran": fran, "alma": alma, "funding": funding, "app": app}


def finalized_run(world, nets, *, held=(), with_accounts=True):
    """A run of the world's pay group whose run employees carry ``nets`` (name -> decimal string), moved to
    FINALIZED, followed by the outbox event that creates the obligations."""
    from app.payroll.services import outbox

    cid = world["cid"]
    if with_accounts:
        for name in nets:
            if not sql("SELECT 1 FROM bank_accounts WHERE company_id = :c AND employee_id = :e",
                       c=cid, e=world["emp"][name]["id"]):
                add_bank_account(cid, world["emp"][name]["id"], NUMBERS[name], holder=f"{name} Test")
    run = new_run(world["admin"], world["group"], world["period"]["id"])
    for name, net in nets.items():
        hold = name in held
        sql("UPDATE payroll_run_employees SET status = 'CALCULATED', exclusion_reason = NULL, exclusion_note = NULL, "
            "gross = :n, net = :n, currency = 'INR', hold_type = :ht, hold = CAST(:h AS jsonb) "
            "WHERE run_id = :r AND employee_id = :e", r=run["id"], e=world["emp"][name]["id"], n=Decimal(net),
            ht="PAYMENT" if hold else None, h='{"reason": "Investigation"}' if hold else None)
    force_status(run["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    with session(cid) as (db, _):
        outbox.emit(db, "payroll.run.finalized", {"run_id": run["id"]})
        db.commit()
    return run["id"]


def instructions_of(run_id):
    return {r[1]: {"id": r[0], "status": r[2], "reason": r[3], "amount": r[4], "batch_id": r[5]} for r in sql(
        "SELECT i.id, i.employee_id, i.status, i.status_reason, i.amount, i.batch_id FROM payment_instructions i "
        "WHERE i.run_id = :r AND i.status NOT IN ('CANCELLED', 'REISSUED') ORDER BY i.id", r=run_id)}


def run_status(run_id):
    return sql("SELECT status FROM payroll_runs WHERE id = :r", r=run_id)[0][0]


def submitted_batch(world, run_id, **create):
    """Create, approve (Alma), generate and release (Fran: separation of duties) a bank-file batch of the run."""
    batch = ok(world["pete"].post(BATCHES, json={"run_id": run_id, **create}), 201)["batches"][0]
    ok(world["alma"].post(f"{BATCHES}/{batch['id']}/approve"))
    ok(world["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"))
    step_up(world["fran"])
    return ok(world["fran"].post(f"{BATCHES}/{batch['id']}/release"))


@pytest.fixture
def pay(app):
    return payments_world(app)


# ---------------------------------------------------------------------------
# Obligations from the finalized-run event (EC-PAY-15, EC-PAY-16)
# ---------------------------------------------------------------------------
def test_ec_pay_15_ec_pay_16_one_obligation_per_positive_net_and_none_for_zero_or_negative(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50", "Zara": "0", "Nell": "-1250.00"})
    rows = sql("SELECT employee_id, amount, currency, status, source_type, run_employee_id FROM payment_obligations "
               "WHERE run_id = :r ORDER BY employee_id", r=run_id)
    by_employee = {r[0]: r for r in rows}
    assert set(by_employee) == {pay["emp"]["Emma"]["id"], pay["emp"]["Ivan"]["id"]}  # EC-PAY-15/16: no Zara, no Nell
    assert by_employee[pay["emp"]["Emma"]["id"]][1:5] == (Decimal("50000.0000"), "INR", "OPEN", "RUN_EMPLOYEE")
    assert by_employee[pay["emp"]["Ivan"]["id"]][1] == Decimal("42000.5000")
    instructions = instructions_of(run_id)
    assert {v["status"] for v in instructions.values()} == {"READY"}
    assert instructions[pay["emp"]["Ivan"]["id"]]["amount"] == Decimal("42000.5000")
    assert "OBLIGATIONS_CREATED" in audit_actions(pay["cid"], "payroll_run", run_id)

    # Repeating the event adds nothing (unique obligation per run employee and currency, unique identity).
    from app.payroll.services import outbox

    with session(pay["cid"]) as (db, _):
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        db.commit()
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=run_id)[0][0] == 2
    assert sql("SELECT count(*) FROM payment_instructions WHERE run_id = :r", r=run_id)[0][0] == 2


def test_obligations_api_lists_masked_instructions_and_ec_pay_23_balances(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    listed = ok(pay["pete"].get(OBLIGATIONS, params={"run_id": run_id}))
    assert listed["total"] == 2
    emma = next(o for o in listed["items"] if o["employee_id"] == pay["emp"]["Emma"]["id"])
    assert emma["amount"] == {"amount": "50000.00", "currency": "INR"}
    assert emma["outstanding"] == {"amount": "50000.00", "currency": "INR"}
    (instruction,) = emma["instructions"]
    assert instruction["beneficiary"]["account_masked"].endswith("4567")
    assert instruction["label"]["text"] == "Scheduled"
    body = str(listed)
    assert NUMBERS["Emma"] not in body and "beneficiary_enc" not in body
    # The sealed snapshot holds the number only encrypted.
    enc = sql("SELECT beneficiary_enc FROM payment_instructions WHERE id = :i", i=instruction["id"])[0][0]
    assert enc and NUMBERS["Emma"] not in enc

    balances = ok(pay["pete"].get(BALANCES, params={"group_by": "run", "run_id": run_id}))["items"]
    assert balances == [{"run_id": run_id, "currency": "INR",
                         "obligated": {"amount": "92000.50", "currency": "INR"},
                         "intended": {"amount": "92000.50", "currency": "INR"},
                         "submitted": {"amount": "0.00", "currency": "INR"},
                         "settled": {"amount": "0.00", "currency": "INR"},
                         "returned": {"amount": "0.00", "currency": "INR"},
                         "outstanding": {"amount": "92000.50", "currency": "INR"}}]
    one = ok(pay["pete"].get(f"{OBLIGATIONS}/{emma['id']}"))
    assert one["id"] == emma["id"] and one["run_employee_id"]


def test_readiness_missing_account_unverified_cooling_off_and_payment_hold(pay):
    cid, emp = pay["cid"], pay["emp"]
    add_bank_account(cid, emp["Ivan"]["id"], NUMBERS["Ivan"], verified=False)  # exclude_unverified is ON
    add_bank_account(cid, emp["Zara"]["id"], NUMBERS["Zara"], usable_hours_ago=-24)  # still cooling off
    add_bank_account(cid, emp["Nell"]["id"], NUMBERS["Nell"])
    add_bank_account(cid, emp["Emma"]["id"], NUMBERS["Emma"])
    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00", "Zara": "300.00", "Nell": "400.00"},
                           held=("Nell",), with_accounts=False)
    rows = instructions_of(run_id)
    assert (rows[emp["Emma"]["id"]]["status"], rows[emp["Emma"]["id"]]["reason"]) == ("READY", None)
    assert (rows[emp["Ivan"]["id"]]["status"], rows[emp["Ivan"]["id"]]["reason"]) == ("ON_HOLD", "UNVERIFIED_ACCOUNT")
    # The only account is approved but still in its cooling-off period (EC-BNK-02): not ready, with the reason.
    assert (rows[emp["Zara"]["id"]]["status"], rows[emp["Zara"]["id"]]["reason"]) == ("NOT_READY", "COOLING_OFF")
    assert (rows[emp["Nell"]["id"]]["status"], rows[emp["Nell"]["id"]]["reason"]) == ("ON_HOLD", "EMPLOYEE_HOLD")

    # The proposal's exclusions report names every left-out payment with its reason (EC-PAY-03).
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    reasons = {e["employee_id"]: e["reason"] for e in proposal["exclusions"]}
    assert reasons == {emp["Ivan"]["id"]: "UNVERIFIED_ACCOUNT", emp["Zara"]["id"]: "COOLING_OFF",
                       emp["Nell"]["id"]: "EMPLOYEE_HOLD"}
    assert proposal["batches"][0]["instruction_ids"] == [rows[emp["Emma"]["id"]]["id"]]

    # A person cannot release a hold whose cause is still there (the account, or the run employee's hold).
    ivan = rows[emp["Ivan"]["id"]]["id"]
    nell = rows[emp["Nell"]["id"]]["id"]
    err(pay["fran"].post(f"{INSTRUCTIONS}/{ivan}/release-hold", json={"reason": "Looks fine"}), 409,
        "HOLD_CAUSE_PRESENT")
    err(pay["fran"].post(f"{INSTRUCTIONS}/{nell}/release-hold", json={"reason": "Investigation closed"}), 409,
        "RUN_EMPLOYEE_HELD")
    err(pay["fran"].post(f"{INSTRUCTIONS}/{nell}/release-hold", json={"reason": ""}), 422)
    # The unverified account is verified later: the system hold lifts on the next readiness check.
    sql("UPDATE bank_accounts SET verification_status = 'VERIFIED', verified_at = now() WHERE employee_id = :e",
        e=emp["Ivan"]["id"])
    ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert instructions_of(run_id)[emp["Ivan"]["id"]]["status"] == "READY"


def test_ec_run_13_releasing_the_run_hold_lifts_the_payment_hold_in_the_same_request(pay):
    from app.payroll.services import outbox

    run_id = finalized_run(pay, {"Emma": "100.00", "Ivan": "200.00"}, held=("Emma",))
    row = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]
    assert row["status"] == "ON_HOLD"
    run_employee = sql("SELECT id FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e", r=run_id,
                       e=pay["emp"]["Emma"]["id"])[0][0]
    with session(pay["cid"]) as (db, _):  # a stale event while the run employee is still held changes nothing
        outbox.emit(db, "payroll.payment_hold_released", {"run_employee_id": run_employee})
        db.commit()
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "ON_HOLD"
    # The held payment is excluded from batches until the hold is released on the run.
    first = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)
    assert [e["reason"] for e in first["exclusions"]] == ["EMPLOYEE_HOLD"]
    # Releasing the hold on the finalized run (runs.create_payment_intent, rebound by the obligations service).
    released = ok(pay["fran"].post(f"{PAY}/runs/{run_id}/employees/{run_employee}/release",
                                   json={"note": "Investigation closed"}))
    assert released["payment_intent"] == {"status": "RELEASED", "released_instructions": 1}
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "READY"
    events = sql("SELECT event_status, actor_user_id FROM payment_events WHERE instruction_id = :i ORDER BY id",
                 i=row["id"])
    assert ("RELEASE_HOLD", pay["fran"].user_id) in [tuple(e) for e in events]
    # The released payment joins the next batch, with its own approval.
    second = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    assert second["item_count"] == 1 and second["status"] == "APPROVAL_PENDING"


def test_ec_run_13_hook_without_held_payments_changes_nothing(pay):
    """The hook is safe to call for a run employee whose payments are not held (or were released already)."""
    from app.payroll.services import obligations

    run_id = finalized_run(pay, {"Emma": "100.00"})
    with session(pay["cid"]) as (db, principal):
        from app.models.pay_runs import PayrollRun, PayrollRunEmployee

        run = db.query(PayrollRun).filter(PayrollRun.id == run_id).one()
        row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run_id,
                                                  PayrollRunEmployee.employee_id == pay["emp"]["Emma"]["id"]).one()
        assert obligations.create_payment_intent(db, principal, run, row) == {"status": "NOTHING_HELD",
                                                                               "released_instructions": 0}
    assert instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["status"] == "READY"


def test_ec_bnk_03_bank_change_after_approval_keeps_the_snapshot_and_offers_recreate(pay):
    run_id = finalized_run(pay, {"Emma": "1000.00", "Ivan": "2000.00"})
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    emma_id = pay["emp"]["Emma"]["id"]
    old = sql("SELECT id FROM bank_accounts WHERE employee_id = :e", e=emma_id)[0][0]
    new = add_bank_account(pay["cid"], emma_id, "501007777777", holder="Emma Test", replaces=old)
    end_bank_account(old, replaced_by=new)

    detail = ok(pay["pete"].get(f"{BATCHES}/{batch['id']}"))
    (warning,) = detail["bank_changes"]
    assert warning["options"] == ["PROCEED_WITH_SNAPSHOT", "RECREATE_INSTRUCTIONS"]
    assert warning["snapshot_account"].endswith("4567")  # the snapshot, never silently redirected
    instruction = next(i for i in detail["instructions"] if i["employee_id"] == emma_id)
    assert instruction["bank_account_id"] == old

    proceed = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/bank-changes",
                                  json={"decision": "PROCEED", "reason": "Confirmed with Emma by phone"}))
    assert proceed["decision"] == "PROCEED" and proceed["batch"]["status"] == "APPROVED"
    assert "BANK_CHANGE_PROCEEDED" in audit_actions(pay["cid"], "payment_batch", batch["id"])

    recreated = ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/bank-changes",
                                    json={"decision": "RECREATE", "reason": "Pay the new account instead"}))
    assert recreated["batch"]["status"] == "CANCELLED"
    (created,) = recreated["created"]
    new_row = sql("SELECT bank_account_id, status, batch_id, amount FROM payment_instructions WHERE id = :i",
                  i=created["id"])[0]
    assert new_row[0] == new and new_row[1] == "READY" and new_row[2] is None and new_row[3] == Decimal("1000.0000")
    assert sql("SELECT status FROM payment_instructions WHERE id = :i", i=instruction["id"])[0][0] == "CANCELLED"
    # The recreated payment needs a new batch and a new approval.
    again = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    assert again["status"] == "APPROVAL_PENDING" and again["item_count"] == 2


def test_my_payments_shows_only_own_payments_with_labels_and_masked_accounts(pay):
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    emma = login_as(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])
    mine = ok(emma.get(MY_PAYMENTS))
    (item,) = mine["items"]
    assert item["amount"] == {"amount": "50000.00", "currency": "INR"} and item["employee_id"] == pay["emp"]["Emma"]["id"]
    (instruction,) = item["instructions"]
    assert instruction["label"]["text"] == "Scheduled"
    assert instruction["beneficiary"]["account_masked"].endswith("4567")
    assert "bank_account_id" not in instruction and "provider_ref" not in instruction
    assert instruction["events"][0]["label"] == "Scheduled"
    assert NUMBERS["Emma"] not in str(mine) and NUMBERS["Ivan"] not in str(mine)
    assert len(mine["years"]) == 1
    assert run_status(run_id) == "FINALIZED"
    # Without an employee record there is nothing to see; payroll endpoints stay closed to employees.
    err(emma.get(OBLIGATIONS), 403)
    err(emma.get(f"{OBLIGATIONS}/{item['id']}"), 403)


def test_payment_permissions_are_function_level_403(pay):
    run_id = finalized_run(pay, {"Emma": "100.00"})
    processor = login_as(pay["app"], pay["admin"], create_employee(pay["admin"], "Pia"), ["payroll_processor"])
    for call in (lambda c: c.get(OBLIGATIONS), lambda c: c.get(BATCHES), lambda c: c.get(BALANCES),
                 lambda c: c.post(BATCHES, json={"run_id": run_id}),
                 lambda c: c.post(f"{BATCHES}/proposal", json={"run_id": run_id})):
        err(call(processor), 403)
    # Unknown fields and JSON numbers for server-owned values are refused (strict models).
    err(pay["pete"].post(BATCHES, json={"run_id": run_id, "status": "SUBMITTED"}), 422)
    err(pay["pete"].post(BATCHES, json={"run_id": run_id, "company_id": 1}), 422)


def test_end_to_end_real_finalization_creates_obligations_that_settle_and_reconcile(app, runs_world, monkeypatch):
    """One path through the real run services: engine calculation, approval and finalization emit
    payroll.run.finalized; the payments subscriber creates one obligation per positive net equal to the finalized
    net; the batch is approved and released (run SUBMITTED) and a bank statement settles and reconciles the run.
    Only the payments subscriber of the event runs here (documents, journals and corrections have their own
    tests)."""
    from app.payroll.services import obligations, outbox
    from tests.payroll.test_reconciliation import csv_statement, upload
    from tests.payroll.test_run_finalize import approved_run, confirm_countries, finalize, user_id_of
    from tests.payroll.test_run_finalize import step_up as session_step_up

    monkeypatch.setitem(outbox.SUBSCRIBERS, "payroll.run.finalized", (obligations.FINALIZED_JOB,))
    w = runs_world
    cid, acme = w["cid"], w["acme"]
    user_id_of(acme)
    session_step_up(acme)
    confirm_countries(cid)
    for name in ("Emma", "Ivan"):
        add_bank_account(cid, w["emp"][name]["id"], NUMBERS[name], holder=f"{name} Test")
    run = approved_run(w)
    finalize(acme, run["id"], key="payments-e2e")
    assert run_status(run["id"]) == "FINALIZED"
    nets = {e: n for e, n in sql("SELECT employee_id, net FROM payroll_run_employees WHERE run_id = :r AND "
                                 "status = 'CALCULATED' AND net > 0", r=run["id"])}
    assert set(nets) == {w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]}
    owed = {e: a for e, a in sql("SELECT employee_id, amount FROM payment_obligations WHERE run_id = :r", r=run["id"])}
    assert owed == nets  # EC-PAY-15/16: exactly the positive finalized nets
    assert {v["status"] for v in instructions_of(run["id"]).values()} == {"READY"}

    pete = login_as(app, acme, create_employee(acme, "Pete", "Preparer"), ["finance_payments"])
    fran = login_as(app, acme, create_employee(acme, "Fran", "Finance"), ["finance_payments"])
    alma = login_as(app, acme, create_employee(acme, "Alma", "Approver"), ["finance_payments"])
    funding = add_funding_account(cid, w["le01"], [fran.user_id])
    submitted_batch({"pete": pete, "fran": fran, "alma": alma}, run["id"])
    assert run_status(run["id"]) == "SUBMITTED"
    lines = [(date.today().isoformat(), f"-{Decimal(amount):.2f}", reference, "") for reference, amount in sql(
        "SELECT reference, amount FROM payment_instructions WHERE run_id = :r ORDER BY id", r=run["id"])]
    result = upload(fran, funding, csv_statement(*lines))
    assert result["import"]["counts"] == {"MATCHED": 2}
    assert run_status(run["id"]) == "RECONCILED"
    assert {r[0] for r in sql("SELECT status FROM payment_obligations WHERE run_id = :r", r=run["id"])} == {"SETTLED"}


# ---------------------------------------------------------------------------
# Another account currency (EC-PAY-17) and redenomination (EC-MON-13, F-09)
# ---------------------------------------------------------------------------
def fx_rate(cid, base, quote, rate, as_of):
    sql("INSERT INTO fx_rates (company_id, base_currency, quote_currency, rate, rate_type, source, as_of, fetched_at, "
        "status, created_at, updated_at) VALUES (:c, :b, :q, :r, 'MID', 'MANUAL', :d, now(), 'ACTIVE', now(), now())",
        c=cid, b=base, q=quote, r=Decimal(rate), d=as_of)


def test_ec_pay_17_account_in_another_currency_waits_for_a_rate_and_records_both_amounts(pay):
    cid, emma = pay["cid"], pay["emp"]["Emma"]["id"]
    add_bank_account(cid, emma, NUMBERS["Emma"], holder="Emma Test", currency="USD")
    run_id = finalized_run(pay, {"Emma": "8300.00"}, with_accounts=False)
    rows = sql("SELECT id, status, status_reason, amount, currency, account_amount, account_currency, "
               "fx_conversion_id FROM payment_instructions WHERE run_id = :r", r=run_id)
    (iid, status, reason, amount, currency, account_amount, account_currency, conversion) = rows[0]
    # Never 1:1: without a rate the payment waits, and is excluded from batches with the reason.
    assert (status, reason, amount, currency) == ("NOT_READY", "FX_RATE_MISSING", Decimal("8300.0000"), "INR")
    assert (account_amount, account_currency, conversion) == (None, None, None)
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert [e["reason"] for e in proposal["exclusions"]] == ["FX_RATE_MISSING"] and proposal["batches"] == []
    assert sql("SELECT status FROM payment_instructions WHERE id = :i", i=iid)[0][0] == "NOT_READY"

    # A rate arrives: the next readiness check converts it and records both amounts and the conversion.
    pay_date = sql("SELECT pay_date FROM pay_periods WHERE id = :p", p=pay["period"]["id"])[0][0]
    fx_rate(cid, "INR", "USD", "0.012", pay_date)
    usd_funding = add_funding_account(cid, pay["le"], [pay["fran"].user_id], number="912020055555555",
                                      name="USD payroll account", currency="USD")
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    (batch,) = proposal["batches"]
    assert batch["currency"] == "USD" and batch["funding_account_id"] == usd_funding
    assert batch["total"] == {"amount": "99.60", "currency": "USD"}
    status, account_amount, account_currency, conversion = sql(
        "SELECT status, account_amount, account_currency, fx_conversion_id FROM payment_instructions WHERE id = :i",
        i=iid)[0]
    assert (status, account_amount, account_currency) == ("READY", Decimal("99.6000"), "USD") and conversion
    detail = ok(pay["pete"].get(f"{INSTRUCTIONS}/{iid}"))
    assert detail["amount"] == {"amount": "8300.00", "currency": "INR"}
    assert detail["account_amount"] == {"amount": "99.60", "currency": "USD"}
    assert detail["fx_conversion_id"] == conversion


def test_ec_pay_20_expired_fx_quote_is_requoted_or_held_and_never_paid_stale(pay):
    """An unsent payment whose FX quote is older than payroll.fx.max_rate_age_hours is re-quoted with a
    current rate before batching (both quotes recorded); without a usable rate it is held, and the hold lifts
    once a rate is approved. A batched payment whose quote expired blocks file generation (the batch is
    cancelled, re-quoted and approved again)."""
    cid, emma = pay["cid"], pay["emp"]["Emma"]["id"]
    add_bank_account(cid, emma, NUMBERS["Emma"], holder="Emma Test", currency="USD")
    pay_date = sql("SELECT pay_date FROM pay_periods WHERE id = :p", p=pay["period"]["id"])[0][0]
    today = date.today()
    for day in sorted({pay_date, today}):
        fx_rate(cid, "INR", "USD", "0.012", day)
    add_funding_account(cid, pay["le"], [pay["fran"].user_id], number="912020055555555", name="USD payroll account",
                        currency="USD")
    run_id = finalized_run(pay, {"Emma": "8300.00"}, with_accounts=False)
    iid, first_quote = sql("SELECT id, fx_conversion_id FROM payment_instructions WHERE run_id = :r", r=run_id)[0]
    assert first_quote and instructions_of(run_id)[emma]["status"] == "READY"

    def expire_quote():
        sql("UPDATE fx_conversions SET created_at = now() - interval '48 hours' WHERE id = (SELECT fx_conversion_id "
            "FROM payment_instructions WHERE id = :i)", i=iid)

    # Expired with a current rate available: re-quoted before batching, both quotes kept in the history.
    expire_quote()
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert [b["instruction_ids"] for b in proposal["batches"]] == [[iid]]
    second_quote = sql("SELECT fx_conversion_id FROM payment_instructions WHERE id = :i", i=iid)[0][0]
    assert second_quote and second_quote != first_quote
    requoted = sql("SELECT details FROM payment_events WHERE instruction_id = :i AND event_status = 'FX_REQUOTED'",
                   i=iid)
    assert requoted and requoted[0][0]["previous_conversion_id"] == first_quote

    # Expired while the latest rate waits for approval: never the stale rate, the payment is held instead.
    expire_quote()
    sql("INSERT INTO fx_rates (company_id, base_currency, quote_currency, rate, rate_type, source, as_of, fetched_at, "
        "status, created_at, updated_at) VALUES (:c, 'INR', 'USD', 0.013, 'MID', 'MANUAL', :d, now() + interval "
        "'1 minute', 'PENDING_APPROVAL', now(), now())", c=cid, d=today)
    proposal = ok(pay["pete"].post(f"{BATCHES}/proposal", json={"run_id": run_id}))
    assert proposal["batches"] == [] and [e["reason"] for e in proposal["exclusions"]] == ["FX_RATE_MISSING"]
    assert instructions_of(run_id)[emma]["status"] == "ON_HOLD"
    # The pending rate is approved: the next readiness check re-quotes and releases the hold by itself.
    sql("UPDATE fx_rates SET status = 'ACTIVE' WHERE company_id = :c AND status = 'PENDING_APPROVAL'", c=cid)
    batch = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    assert batch["total"] == {"amount": "107.90", "currency": "USD"}  # 8300 x 0.013, the current rate

    # Approved, then the quote expires before the file is generated: refused, nothing queued at a stale rate.
    ok(pay["fran"].post(f"{BATCHES}/{batch['id']}/approve"))
    expire_quote()
    err(pay["pete"].post(f"{BATCHES}/{batch['id']}/generate-file"), 409, "FX_QUOTE_EXPIRED")
    assert instructions_of(run_id)[emma]["status"] == "APPROVED"
    ok(pay["pete"].post(f"{BATCHES}/{batch['id']}/cancel", json={"reason": "Re-quote the exchange rate"}))
    again = ok(pay["pete"].post(BATCHES, json={"run_id": run_id}), 201)["batches"][0]
    assert again["status"] == "APPROVAL_PENDING" and again["item_count"] == 1


def test_ec_mon_13_redenomination_never_rewrites_payment_history(pay):
    """F-09: payment records carry their own currency; a later base-currency change of the legal entity
    (redenomination, effective-dated) leaves obligations, instructions, batches and balances untouched."""
    run_id = finalized_run(pay, {"Emma": "50000.00", "Ivan": "42000.50"})
    batch = submitted_batch(pay, run_id)
    before = sql("SELECT id, amount, currency FROM payment_obligations WHERE run_id = :r ORDER BY id", r=run_id)
    instructions = sql("SELECT id, amount, currency, status FROM payment_instructions WHERE run_id = :r ORDER BY id",
                       r=run_id)
    sql("UPDATE legal_entities SET base_currency = 'AED' WHERE id = :e", e=pay["le"])
    assert sql("SELECT id, amount, currency FROM payment_obligations WHERE run_id = :r ORDER BY id",
               r=run_id) == before
    assert sql("SELECT id, amount, currency, status FROM payment_instructions WHERE run_id = :r ORDER BY id",
               r=run_id) == instructions
    detail = ok(pay["pete"].get(f"{BATCHES}/{batch['id']}"))
    assert detail["currency"] == "INR" and detail["total"] == {"amount": "92000.50", "currency": "INR"}
    balances = ok(pay["pete"].get(BALANCES, params={"group_by": "legal_entity"}))["items"]
    assert [(b["currency"], b["obligated"]["amount"]) for b in balances] == [("INR", "92000.50")]
    # The regenerated bank file still reproduces its stored hash (amounts and currency unchanged).
    download = pay["fran"].get(f"{BATCHES}/{batch['id']}/file")
    assert download.status_code == 200 and download.headers["x-content-sha256"] == detail["file"]["sha256"]
