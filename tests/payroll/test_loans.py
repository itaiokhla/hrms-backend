"""Loans and salary advances: policy checks, schedules, approvals, the protected-net activation gate, the
append-only ledger with one instalment per run employee, approved actions, self-service and scope
(3.4 §15, 3.1 §5.11, 3.9 §3.5/§4; decision I-13, G-23), plus row-level isolation across three tenants.

EC-VAR-06, EC-VAR-08, EC-SEC-01, EC-SEC-05.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.helpers import create_employee
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    CHECK,
    DENIED,
    FOREIGN_KEY,
    UNIQUE,
    activate_policy,
    audit_actions,
    expect_error,
    month_of,
    pay_group,
    place,
    session,
    set_setting,
    sql,
    tenant,
    world,
)

STAFF = "/api/v1/payroll/loans"
MY = "/api/v1/me/payroll/loans"
LOAN_POLICY = {"max_amount_formula": "3 * MONTHLY_GROSS", "max_tenure_months": 12, "interest_rate_percent": "12",
               "interest_method": "REDUCING", "max_instalment_percent_of_net": "50", "min_service_months": 6,
               "max_active_loans": 1}
ADVANCE_POLICY = {"max_percent_of_monthly_net": "50", "recover_in_runs": 2, "max_per_year": 1}
GROSS, NET = Decimal("50000"), Decimal("40000")


def pay_figures(*values):
    """Monthly gross and net for the pay-figures hook of the loans service."""
    from app.payroll.services.loans import PayFigures

    return PayFigures(*values)


@pytest.fixture
def loans_world(world):
    from app.payroll.services import loans

    cid = world["cid"]
    set_setting(cid, "payroll.loans.enabled", True)
    set_setting(cid, "payroll.advances.enabled", True)
    founder = world["u"]["founder"]
    activate_policy(founder, "LOAN_POLICY", LOAN_POLICY)
    activate_policy(founder, "ADVANCE_POLICY", ADVANCE_POLICY)
    figures = {"value": pay_figures(GROSS, NET)}
    previous = loans.set_pay_figures_provider(lambda db, c, e, on, currency: figures["value"])
    world["figures"] = figures
    yield world
    loans.set_pay_figures_provider(previous)


def loan_body(w, employee="Emma", amount="60000.00", currency="INR", kind="LOAN", tenure=6, **extra):
    data = {"employee_id": w["emp"][employee], "kind": kind, "amount": {"amount": amount, "currency": currency},
            **extra}
    if tenure is not None:
        data["tenure"] = tenure
    return data


def apply(client, data, url=STAFF):
    r = client.post(url, json=data)
    assert r.status_code == 201, r.text
    return r.json()


def get(client, loan_id, url=STAFF):
    r = client.get(f"{url}/{loan_id}")
    assert r.status_code == 200, r.text
    return r.json()


def approve(client, loan_id, **body):
    return client.post(f"{STAFF}/{loan_id}/approve", json=body)


def disburse(company_id, loan_id, reference="PAY-1", paid_on=None):
    from app.payroll.services import loans

    with session(company_id) as (db, _):
        loan = loans.record_disbursement(db, None, company_id, loan_id, paid_on=paid_on or loans.company_today(
            db, company_id), reference=reference)
        db.commit()
        return loan.status


def active_loan(w, employee="Emma", amount="60000.00", tenure=6):
    set_setting(w["cid"], "payroll.deductions.min_net_pay_percent", "60")
    loan = apply(w["u"]["pia"], loan_body(w, employee, amount, tenure=tenure))
    assert approve(w["u"]["founder"], loan["id"]).status_code == 200
    assert disburse(w["cid"], loan["id"], reference=f"PAY-{loan['id']}") == "ACTIVE"
    return loan["id"]


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
def test_loan_application_is_scheduled_from_the_policy_and_approved_by_another_user(loans_world):
    w = loans_world
    pia, founder = w["u"]["pia"], w["u"]["founder"]
    loan = apply(pia, loan_body(w, purpose="Medical"))
    assert loan["status"] == "PENDING_APPROVAL" and loan["approval_request_id"]
    assert "PROTECTED_NET_NOT_SET" in loan["issues"]
    row = get(pia, loan["id"])
    assert row["principal"] == {"amount": "60000.00", "currency": "INR"} and row["tenure"] == 6
    assert row["schedule"]["method"] == "REDUCING" and len(row["schedule"]["instalments"]) == 6
    assert row["schedule"]["total_payable"] == "62117.41"
    assert row["balance"] == {"amount": "0.00", "currency": "INR"}  # nothing outstanding before disbursement
    assert row["interest"]["rate_percent"] == "12" and row["recover_balance_on_exit"] is True
    assert sql("SELECT module FROM approval_requests WHERE id = :i", i=loan["approval_request_id"]) == [("loan",)]

    assert approve(pia, loan["id"]).status_code == 403  # the maker never approves their own application
    r = approve(founder, loan["id"], remarks="ok")
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert approve(founder, loan["id"]).status_code == 409
    assert audit_actions(w["cid"], "loan", loan["id"])[:2] == ["LOAN_APPLIED", "LOAN_APPROVED"]
    r = pia.post(f"{STAFF}/{loan['id']}/cancel", json={"reason": "Not needed any more"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text
    assert disburse_refused(w["cid"], loan["id"]) == 409


def disburse_refused(company_id, loan_id):
    from app.core.errors import AppError

    with pytest.raises(AppError) as info:
        disburse(company_id, loan_id)
    return info.value.status_code


def test_policy_checks_refuse_applications_that_break_the_rules(loans_world):
    w = loans_world
    pia, founder = w["u"]["pia"], w["u"]["founder"]
    cases = (
        (loan_body(w, amount="150000.01"), 409, "LOAN_ABOVE_MAXIMUM"),  # 3 x monthly gross
        (loan_body(w, tenure=13), 409, "LOAN_TENURE_ABOVE_MAXIMUM"),
        (loan_body(w, amount="150000.00"), 409, "LOAN_INSTALMENT_ABOVE_NET_SHARE"),  # > 50 % of net
        (loan_body(w, currency="USD"), 400, "CURRENCY_MISMATCH"),
    )
    for data, status, code in cases:
        r = pia.post(STAFF, json=data)
        assert (r.status_code, r.json().get("code")) == (status, code), r.text
    w["figures"]["value"] = pay_figures()  # monthly gross unknown: fail closed
    r = pia.post(STAFF, json=loan_body(w))
    assert (r.status_code, r.json()["code"]) == (409, "LOAN_MAXIMUM_UNKNOWN")
    w["figures"]["value"] = pay_figures(GROSS, NET)

    newcomer = create_employee(founder, "Nina", hire_date=(w["today"] - timedelta(days=40)).isoformat())
    place(newcomer["id"], w["le01"]["id"], w["groups"]["IN"])
    r = pia.post(STAFF, json={**loan_body(w), "employee_id": newcomer["id"]})
    assert (r.status_code, r.json()["code"]) == (409, "LOAN_SERVICE_TOO_SHORT")

    apply(pia, loan_body(w))
    r = pia.post(STAFF, json=loan_body(w, amount="1000.00"))
    assert (r.status_code, r.json()["code"]) == (409, "LOAN_TOO_MANY_ACTIVE")

    data = loan_body(w, employee="Ivan")
    data["amount"] = {"amount": 1000, "currency": "INR"}
    assert pia.post(STAFF, json=data).status_code == 422  # a JSON number is never money
    for extra in ({"status": "ACTIVE"}, {"schedule": {}}, {"company_id": w["cid"]}, {"interest_rate_percent": "0"}):
        assert pia.post(STAFF, json=loan_body(w, employee="Ivan", **extra)).status_code == 422, extra
    set_setting(w["cid"], "payroll.loans.enabled", False)
    r = pia.post(STAFF, json=loan_body(w, employee="Ivan"))
    assert (r.status_code, r.json()["code"]) == (409, "LOANS_DISABLED")


# ---------------------------------------------------------------------------
# Activation and the ledger
# ---------------------------------------------------------------------------
def test_g23_activation_is_refused_while_the_protected_net_percentage_is_unset(loans_world):
    from app.core.errors import AppError

    w = loans_world
    cid = w["cid"]
    pending = apply(w["u"]["pia"], loan_body(w, employee="Ivan"))
    assert disburse_refused(cid, pending["id"]) == 409  # not approved yet
    loan = apply(w["u"]["pia"], loan_body(w))
    approve(w["u"]["founder"], loan["id"])
    with pytest.raises(AppError) as info:
        disburse(cid, loan["id"])
    assert info.value.code == "PROTECTED_NET_REQUIRED"
    assert get(w["u"]["pia"], loan["id"])["status"] == "APPROVED"

    set_setting(cid, "payroll.deductions.min_net_pay_percent", "60")
    assert disburse(cid, loan["id"], reference="BANK-77") == "ACTIVE"
    assert disburse(cid, loan["id"], reference="BANK-77") == "ACTIVE"  # a repeated event is a no-op
    row = get(w["u"]["pia"], loan["id"])
    assert row["disbursed_on"] == w["today"].isoformat()
    assert [(t["kind"], t["amount"]["amount"], t["reference"]) for t in row["transactions"]] == [
        ("DISBURSEMENT", "60000.00", "BANK-77")]
    assert row["balance"] == {"amount": "62117.41", "currency": "INR"}  # principal + interest of the schedule
    assert "LOAN_DISBURSED" in audit_actions(cid, "loan", loan["id"])
    with tenant(cid) as conn:
        expect_error(conn, UNIQUE, "INSERT INTO loan_transactions (company_id, loan_id, kind, amount, currency, "
                     "effective_on) VALUES (:c, :l, 'DISBURSEMENT', 1, 'INR', now())", c=cid, l=loan["id"])


def test_ec_var_08_one_instalment_per_run_employee_and_the_balance_comes_from_the_ledger(loans_world):
    from app.core.errors import AppError
    from app.payroll.services import loans

    w = loans_world
    cid = w["cid"]
    loan_id = active_loan(w)
    start, end = month_of(w["today"])
    with session(cid) as (db, _):
        [due] = loans.loan_instalments_due(db, cid, w["emp"]["Emma"], start, end)
        assert (due["loan_id"], due["component"], due["status"], due["balance"]) == (
            loan_id, "LOAN_RECOVERY", "ACTIVE", "62117.41")
        assert due["next_instalment"] == {"number": 1, "amount": "10352.90"}
        assert [t["kind"] for t in due["transactions"]] == ["DISBURSEMENT"] and len(due["version_hash"]) == 64
        assert loans.loan_instalments_due(db, cid, w["emp"]["Emma"], start - timedelta(days=400),
                                          start - timedelta(days=370)) == []  # not disbursed by then
        first = loans.post_instalment(db, cid, loan_id, 9001, Decimal("10352.90"), scheduled=Decimal("10352.90"),
                                      effective_on=end)
        again = loans.post_instalment(db, cid, loan_id, 9001, Decimal("10352.90"), scheduled=Decimal("10352.90"),
                                      effective_on=end)
        assert again.id == first.id  # a regenerated or retried run never recovers twice
        with pytest.raises(AppError) as info:
            loans.post_instalment(db, cid, loan_id, 9001, Decimal("500.00"), scheduled=None, effective_on=end)
        assert info.value.code == "INSTALMENT_EXISTS"
        db.rollback()
        loans.post_instalment(db, cid, loan_id, 9001, Decimal("10352.90"), scheduled=Decimal("10352.90"),
                              effective_on=end)
        with pytest.raises(AppError) as info:
            loans.post_instalment(db, cid, loan_id, 9002, Decimal("99999.00"), scheduled=None, effective_on=end)
        assert info.value.code == "AMOUNT_ABOVE_BALANCE"
        db.rollback()
        loans.post_instalment(db, cid, loan_id, 9001, Decimal("10352.90"), scheduled=Decimal("10352.90"),
                              effective_on=end)
        db.commit()
    row = get(w["u"]["pia"], loan_id)
    assert row["balance"]["amount"] == "51764.51"
    assert [t["kind"] for t in row["transactions"]] == ["DISBURSEMENT", "INSTALMENT"]
    with session(cid) as (db, _):
        assert loans.loan_instalments_due(db, cid, w["emp"]["Emma"], start, end)[0]["next_instalment"]["number"] == 2
    tx_id = row["transactions"][1]["id"]
    with tenant(cid) as conn:
        expect_error(conn, UNIQUE, "INSERT INTO loan_transactions (company_id, loan_id, kind, amount, currency, "
                     "run_employee_id, effective_on) VALUES (:c, :l, 'INSTALMENT', 1, 'INR', 9001, now())",
                     c=cid, l=loan_id)
        expect_error(conn, CHECK, "INSERT INTO loan_transactions (company_id, loan_id, kind, amount, currency, "
                     "effective_on) VALUES (:c, :l, 'INSTALMENT', 1, 'INR', now())", c=cid, l=loan_id)
        expect_error(conn, CHECK, "UPDATE loan_transactions SET amount = 1 WHERE id = :i", i=tx_id)  # append-only
        expect_error(conn, CHECK, "DELETE FROM loan_transactions WHERE id = :i", i=tx_id)
    assert "LOAN_INSTALMENT_POSTED" in audit_actions(cid, "loan", loan_id)


# ---------------------------------------------------------------------------
# Approved actions (EC-VAR-06)
# ---------------------------------------------------------------------------
def act(client, loan_id, action, **body):
    return client.post(f"{STAFF}/{loan_id}/{action}", json=body)


def test_ec_var_06_pause_resume_restructure_prepay_and_waive_are_applied_only_when_approved(loans_world):
    from app.payroll.services import loans

    w = loans_world
    cid = w["cid"]
    pia, founder = w["u"]["pia"], w["u"]["founder"]
    pending = apply(pia, loan_body(w, employee="Ivan"))
    assert act(pia, pending["id"], "pause", reason="x").status_code == 409  # only a running loan pauses
    loan_id = active_loan(w)

    r = act(pia, loan_id, "pause", reason="Medical leave")
    assert r.status_code == 202 and r.json()["status"] == "PENDING_APPROVAL", r.text
    assert get(pia, loan_id)["status"] == "ACTIVE"  # nothing changes before the approval
    r = act(pia, loan_id, "prepay", amount={"amount": "1000.00", "currency": "INR"})
    assert (r.status_code, r.json()["code"]) == (409, "ACTION_PENDING")
    assert approve(pia, loan_id).status_code == 403
    assert approve(founder, loan_id).status_code == 200
    assert get(pia, loan_id)["status"] == "PAUSED"
    start, end = month_of(w["today"])
    with session(cid) as (db, _):
        assert loans.loan_instalments_due(db, cid, w["emp"]["Emma"], start, end)[0]["status"] == "PAUSED"

    assert act(pia, loan_id, "resume").status_code == 202
    assert approve(founder, loan_id).status_code == 200
    assert get(pia, loan_id)["status"] == "ACTIVE"
    with session(cid) as (db, _):
        loans.post_instalment(db, cid, loan_id, 9101, Decimal("10352.90"), scheduled=Decimal("10352.90"),
                              effective_on=end)
        db.commit()

    assert act(pia, loan_id, "restructure", reason="Lower instalments", tenure=0).status_code == 422
    assert act(pia, loan_id, "restructure", reason="Lower instalments", tenure=3).status_code == 202
    assert approve(founder, loan_id).status_code == 200
    row = get(pia, loan_id)
    assert (row["schedule_version"], row["tenure"], row["balance"]["amount"]) == (2, 3, "51764.51")
    assert row["schedule"]["principal"] == "51764.51" and len(row["schedule"]["instalments"]) == 3
    assert row["schedule"]["total_interest"] == "0.00"  # a restructure adds no new interest

    r = act(pia, loan_id, "prepay", amount={"amount": "51764.52", "currency": "INR"})
    assert (r.status_code, r.json()["code"]) == (400, "AMOUNT_ABOVE_BALANCE")
    r = act(pia, loan_id, "prepay", amount={"amount": "20000.00", "currency": "INR"}, reference="RCPT-9")
    assert r.status_code == 202, r.text
    assert approve(founder, loan_id).status_code == 200
    row = get(pia, loan_id)
    assert row["balance"]["amount"] == "31764.51"
    prepayment = row["transactions"][-1]
    assert (prepayment["kind"], prepayment["reference"], prepayment["schedule_version"]) == ("PREPAYMENT", "RCPT-9", 2)

    assert act(pia, loan_id, "waive", amount={"amount": "100.00", "currency": "INR"}).status_code == 422  # reason
    assert act(pia, loan_id, "waive", reason="Goodwill", amount={"amount": "100.00", "currency": "INR"}
               ).status_code == 202
    r = founder.post(f"{STAFF}/{loan_id}/reject", json={"remarks": "No waiver"})
    assert r.status_code == 200, r.text
    row = get(pia, loan_id)
    assert row["balance"]["amount"] == "31764.51" and row["actions"][-1]["status"] == "REJECTED"

    act(pia, loan_id, "waive", reason="Hardship", amount={"amount": "31764.51", "currency": "INR"})
    assert approve(founder, loan_id).status_code == 200
    row = get(pia, loan_id)
    assert (row["status"], row["balance"]["amount"]) == ("CLOSED", "0.00") and row["closed_at"]
    assert [a["status"] for a in row["actions"]] == ["APPLIED"] * 4 + ["REJECTED", "APPLIED"]
    assert act(pia, loan_id, "pause", reason="late").status_code == 409
    actions = audit_actions(cid, "loan", loan_id)
    assert actions.count("LOAN_ACTION_REQUESTED") == 6 and actions.count("LOAN_ACTION_APPLIED") == 5
    assert "LOAN_ACTION_REJECTED" in actions


# ---------------------------------------------------------------------------
# Advances and self-service
# ---------------------------------------------------------------------------
def test_advances_are_capped_by_monthly_net_and_the_yearly_count(loans_world):
    w = loans_world
    emma = w["u"]["emma"]
    mine = {"kind": "ADVANCE", "amount": {"amount": "15000.00", "currency": "INR"}}
    r = emma.post(MY, json={**mine, "amount": {"amount": "20000.01", "currency": "INR"}})
    assert (r.status_code, r.json()["code"]) == (409, "ADVANCE_ABOVE_MAXIMUM")  # 50 % of 40,000
    assert emma.post(MY, json={**mine, "tenure": 3}).status_code == 400  # the policy recovers in at most 2 runs
    w["figures"]["value"] = pay_figures(GROSS, None)
    r = emma.post(MY, json=mine)
    assert (r.status_code, r.json()["code"]) == (409, "ADVANCE_NET_UNKNOWN")
    w["figures"]["value"] = pay_figures(GROSS, NET)

    advance = apply(emma, mine, url=MY)
    row = get(emma, advance["id"], url=MY)
    assert (row["kind"], row["tenure"], row["schedule"]["method"]) == ("ADVANCE", 2, "EQUAL")
    assert [i["total"] for i in row["schedule"]["instalments"]] == ["7500.00", "7500.00"]
    r = emma.post(MY, json={**mine, "amount": {"amount": "100.00", "currency": "INR"}})
    assert (r.status_code, r.json()["code"]) == (409, "ADVANCE_YEARLY_LIMIT")
    assert approve(w["u"]["cleo"], advance["id"]).status_code == 200


def test_ec_sec_05_employees_apply_for_their_own_and_payroll_staff_act_in_scope(loans_world):
    w = loans_world
    emma, gus, cleo, pia, beta = (w["u"][k] for k in ("emma", "gus", "cleo", "pia", "beta"))
    mine = apply(emma, {"kind": "LOAN", "amount": {"amount": "30000.00", "currency": "INR"}, "tenure": 6}, url=MY)
    assert get(emma, mine["id"], url=MY)["employee_id"] == w["emp"]["Emma"]
    gulf = apply(pia, loan_body(w, employee="Gus", amount="9000.00", currency="AED"))
    ivan = apply(pia, loan_body(w, employee="Ivan", amount="9000.00"))

    assert emma.get(f"{MY}/{ivan['id']}").status_code == 404
    assert [loan["id"] for loan in emma.get(MY).json()["items"]] == [mine["id"]]
    assert {loan["id"] for loan in emma.get(STAFF).json()["items"]} == {mine["id"]}  # SELF read only
    assert emma.post(STAFF, json=loan_body(w, employee="Ivan")).status_code == 403
    assert emma.post(MY, json={**loan_body(w), "kind": "LOAN"}).status_code == 422  # employee_id is not hers to set
    assert approve(emma, mine["id"]).status_code == 403
    assert gus.get(f"{MY}/{mine['id']}").status_code == 404

    assert {loan["id"] for loan in cleo.get(STAFF).json()["items"]} == {mine["id"], ivan["id"]}
    assert cleo.get(f"{STAFF}/{gulf['id']}").status_code == 404
    assert approve(cleo, gulf["id"]).status_code == 404
    assert act(cleo, gulf["id"], "pause", reason="x").status_code == 404
    assert cleo.post(STAFF, json=loan_body(w, employee="Gus", amount="10.00", currency="AED")).status_code == 404
    assert approve(cleo, mine["id"]).status_code == 200
    assert approve(pia, gulf["id"]).status_code == 403  # Pia applied for it
    assert approve(w["u"]["founder"], gulf["id"]).status_code == 200

    assert beta.get(f"{STAFF}/{mine['id']}").status_code == 404
    assert beta.get(STAFF).json()["total"] == 0
    assert approve(beta, mine["id"]).status_code == 404


# ---------------------------------------------------------------------------
# Row-level isolation (three tenants)
# ---------------------------------------------------------------------------
TABLES = ("loans", "loan_actions", "loan_transactions")


def test_ec_sec_01_loan_tables_are_isolated_across_three_tenants(world):
    from app.models import TENANT_TABLES

    assert set(TABLES) <= set(TENANT_TABLES)
    companies = [world["cid"], world["u"]["beta"].company_id, world["u"]["cobalt"].company_id]
    seeded = {}
    for index, cid in enumerate(companies):
        employee = world["emp"]["Emma"] if index == 0 else create_employee(
            world["u"]["beta" if index == 1 else "cobalt"], "Iso")["id"]
        entity = sql("SELECT id FROM legal_entities WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
        group = world["groups"]["IN"] if index == 0 else pay_group(cid, entity, f"ISO{index}")
        if index:
            place(employee, entity, group)
        version = sql("SELECT id FROM policy_versions WHERE company_id = :c ORDER BY id LIMIT 1", c=cid)[0][0]
        with tenant(cid) as conn:
            loan = conn.execute(text(
                "INSERT INTO loans (company_id, employee_id, legal_entity_id, pay_group_id, kind, principal, currency, "
                "tenure, schedule, policy_version_id, status, disbursed_on) VALUES (:c, :e, :le, :pg, 'LOAN', 100, "
                "'INR', 2, '{}'::jsonb, :v, 'ACTIVE', '2026-10-01') RETURNING id"),
                {"c": cid, "e": employee, "le": entity, "pg": group, "v": version}).scalar()
            action = conn.execute(text(
                "INSERT INTO loan_actions (company_id, loan_id, action, reason) VALUES (:c, :l, 'PAUSE', 'x') "
                "RETURNING id"), {"c": cid, "l": loan}).scalar()
            tx = conn.execute(text(
                "INSERT INTO loan_transactions (company_id, loan_id, kind, amount, currency, effective_on) "
                "VALUES (:c, :l, 'DISBURSEMENT', 100, 'INR', '2026-10-01') RETURNING id"), {"c": cid, "l": loan}).scalar()
        seeded[cid] = {"loans": loan, "loan_actions": action, "loan_transactions": tx, "employee": employee,
                       "entity": entity, "group": group, "version": version}

    with tenant(None) as conn:
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"), {"i": foreign}).scalar() == 0
                    assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                        {"i": foreign}).rowcount == 0
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": foreign}).rowcount == 0
                # The ledger's append-only trigger refuses any update before row-level security is checked.
                expect_error(conn, CHECK if table == "loan_transactions" else DENIED,
                             f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0], i=seeded[cid][table])
    a, b = companies[0], companies[1]
    with tenant(a) as conn:  # composite keys refuse another tenant's loan, employee or policy version
        expect_error(conn, FOREIGN_KEY, "INSERT INTO loan_transactions (company_id, loan_id, kind, amount, currency, "
                     "effective_on) VALUES (:c, :l, 'WAIVER', 1, 'INR', '2026-10-02')", c=a, l=seeded[b]["loans"])
        expect_error(conn, FOREIGN_KEY, "INSERT INTO loan_actions (company_id, loan_id, action) VALUES (:c, :l, "
                     "'RESUME')", c=a, l=seeded[b]["loans"])
        for column in ("employee", "version"):
            values = {k: seeded[a][k] for k in ("employee", "entity", "group", "version")}
            values[column] = seeded[b][column]
            expect_error(conn, FOREIGN_KEY,
                         "INSERT INTO loans (company_id, employee_id, legal_entity_id, pay_group_id, kind, principal, "
                         "currency, tenure, schedule, policy_version_id) VALUES (:c, :e, :le, :pg, 'LOAN', 100, 'INR', "
                         "2, '{}'::jsonb, :v)", c=a, e=values["employee"], le=values["entity"], pg=values["group"],
                         v=values["version"])
