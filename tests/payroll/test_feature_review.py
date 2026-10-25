"""Adversarial review (security and money) of nominees, legal holds, tax profiles and unclaimed pay.

Each test pins a defect found in review (and now fixed) or a hole the feature tests did not close:

* an approved reissue slipping a READY payment past the policy hold of UNCLAIMED pay,
* UNCLAIMED pay of a deceased employee's settlement telling the deceased employee's own login,
* a login of the deceased employee recording, approving or ending the payees of their own settlement,
* a company retention setting below the documented floor shortening how long payslip PDFs are kept.

Helpers and fixtures come from the feature test modules (one database, one lock: run through pytest_serial).
"""

from datetime import date

import pytest

from tests.helpers import invite_and_activate
from tests.payroll.test_bank_accounts import CHANGES, step_up
from tests.payroll.test_compensation import login_as
from tests.payroll.test_legal_holds import (  # noqa: F401  (docs_world is a fixture)
    docs_world,
    emma_run,
    generate,
    payslip_of,
)
from tests.payroll.test_nominees import (  # noqa: F401  (nom_world and its fixtures)
    NOM,
    approve_change,
    death_case,
    fs_world,
    make_nominee,
    nom_world,
    nominee_account,
    retro_world,
    runs_world,
    settle_all,
)
from tests.payroll.test_unclaimed import (  # noqa: F401  (pay and its fixtures)
    INSTRUCTIONS,
    age,
    err,
    instructions_of,
    notifications_of,
    obligation_of,
    ok,
    pay,
    payments_world,
    prepare,
    scan_api,
)
from tests.payroll.test_wage_inputs import sql


# ---------------------------------------------------------------------------
# Unclaimed pay: no READY payment inside an UNCLAIMED obligation
# ---------------------------------------------------------------------------
def test_a_reissue_approved_after_the_obligation_became_unclaimed_is_refused(pay):
    """A failed payment's reissue was requested (approval pending) before the policy scan marked the obligation
    UNCLAIMED. Approving it afterwards must not create a READY payment that a batch could pick up: the obligation
    is released as a whole, with a reason and a step-up, or nothing moves."""
    run_id = prepare(pay)
    emma = pay["emp"]["Emma"]["id"]
    emma_obligation = obligation_of(pay, run_id, "Emma")[0]
    instruction = instructions_of(run_id)[emma]["id"]
    sql("UPDATE payment_instructions SET status = 'FAILED', failure_code = 'AC04', failure_message = 'Account closed' "
        "WHERE id = :i", i=instruction)
    requested = ok(pay["pete"].post(f"{INSTRUCTIONS}/{instruction}/reissue", json={"reason": "Account closed"}))
    assert requested["status"] == "APPROVAL_PENDING"
    age(emma_obligation, 60)
    assert [m["obligation_id"] for m in scan_api(pay["alma"])["marked"]] == [emma_obligation]
    assert obligation_of(pay, run_id, "Emma")[1] == "UNCLAIMED"
    # Another tenant's finance user cannot even see the instruction (three tenants).
    other = payments_world(pay["app"], name="Other Pay")
    assert other["fran"].post(f"{INSTRUCTIONS}/{instruction}/reissue/approve", json={"note": "x"}).status_code == 404
    # The approver of the same tenant is refused while the obligation is unclaimed; nothing was created.
    err(pay["fran"].post(f"{INSTRUCTIONS}/{instruction}/reissue/approve", json={"note": "New account verified"}), 409,
        "OBLIGATION_UNCLAIMED")
    assert sql("SELECT count(*) FROM payment_instructions WHERE obligation_id = :o AND status = 'READY'",
               o=emma_obligation)[0][0] == 0
    assert sql("SELECT count(*) FROM payment_instructions WHERE obligation_id = :o", o=emma_obligation)[0][0] == 1
    # After the obligation is released (step-up, reason) the same approval works: the control is not a dead end.
    step_up(pay["alma"])
    ok(pay["alma"].post(f"/api/v1/payroll/unclaimed-payments/{emma_obligation}/release",
                        json={"reason": "Employee confirmed a new account"}))
    approved = ok(pay["fran"].post(f"{INSTRUCTIONS}/{instruction}/reissue/approve", json={"note": "New account verified"}))
    assert approved["status"] == "REISSUED"


def test_unclaimed_pay_of_a_settlement_paid_to_nominees_never_tells_the_deceased_employees_login(pay):
    """The obligation of a DEATH settlement has payee type NOMINEE. Marking it UNCLAIMED tells finance, not the
    deceased employee's own login (the employee-facing 'a payment to you is on hold' stays for employees)."""
    emma = invite_and_activate(pay["app"], pay["admin"], pay["emp"]["Emma"], ["employee"])
    ivan = invite_and_activate(pay["app"], pay["admin"], pay["emp"]["Ivan"], ["employee"])
    run_id = prepare(pay)
    emma_obligation = obligation_of(pay, run_id, "Emma")[0]
    ivan_obligation = obligation_of(pay, run_id, "Ivan")[0]
    sql("UPDATE payment_obligations SET payee_type = 'NOMINEE' WHERE id = :i", i=emma_obligation)
    age(emma_obligation, 45)
    age(ivan_obligation, 45)
    outcome = scan_api(pay["alma"])
    assert sorted(m["obligation_id"] for m in outcome["marked"]) == sorted([emma_obligation, ivan_obligation])
    assert "A payment to you is on hold" not in notifications_of(emma)    # the deceased: nothing about this pay
    assert "A payment to you is on hold" in notifications_of(ivan)         # an employee: told, as before
    assert "Unclaimed pay" in notifications_of(pay["alma"])                # finance is told about both


# ---------------------------------------------------------------------------
# Nominees: the deceased employee's own login never decides the estate's payees
# ---------------------------------------------------------------------------
def test_a_login_of_the_deceased_employee_cannot_record_approve_or_end_the_payees_of_their_settlement(nom_world):
    w = nom_world
    data = settle_all(w, death_case(w, settle=False)["id"])
    nominee = make_nominee(w["maker"], data["id"], "Asha Nair", "60")
    change = nominee_account(w["maker"], nominee["id"], "Asha Nair", "50100123456701")
    # Ivan (the deceased) still has a login with both the processor and the approver rights, company-wide.
    ivan = login_as(w["app"], w["acme"], w["emp"]["Ivan"], ["payroll_processor", "payroll_approver"])
    step_up(ivan)
    refused = ivan.post(f"{CHANGES}/{change['id']}/approve", json={})
    assert refused.status_code == 403, refused.text
    assert sql("SELECT status FROM bank_account_change_requests WHERE id = :i", i=change["id"])[0][0] == "PENDING_APPROVAL"
    make_nominee(ivan, data["id"], "Bram Nair", "40", relationship="Son", expect=403)
    nominee_account(ivan, nominee["id"], "Asha Nair", "50100123456702", expect=403)
    step_up(ivan)
    assert ivan.patch(f"{NOM}/{nominee['id']}", json={"share_percent": "50"}).status_code == 403
    step_up(ivan)
    assert ivan.post(f"{NOM}/{nominee['id']}/end", json={"reason": "Not mine to decide"}).status_code == 403
    assert sql("SELECT share_percent, status FROM nominees WHERE id = :i", i=nominee["id"])[0] == (60, "ACTIVE")
    # Someone else holding the same rights still works (the control is about the deceased, not the role)...
    approve_change(w["checker"], change["id"])
    # ...and another tenant's staff never reach the change (three tenants).
    for tenant_world in (w["beta"], w["cobalt"]):
        client = tenant_world["client"]
        step_up(client)
        assert client.post(f"{CHANGES}/{change['id']}/approve", json={}).status_code in (403, 404)


# ---------------------------------------------------------------------------
# Retention: the documented floor
# ---------------------------------------------------------------------------
def test_a_retention_setting_below_the_documented_floor_never_shortens_how_long_pdfs_are_kept(docs_world):
    """3.1 §5.19: 'cannot be set below the pack minimum (8 if unknown)'. No pack carries a sourced minimum, so a
    company value below 8 years (here 1) leaves the retention date at 8 years; 10 years is honoured."""
    from app.payroll.services.legal_holds import add_years

    w = docs_world
    cid = w["cid"]

    def set_years(years):
        sql("DELETE FROM setting_values WHERE company_id = :c AND key = 'privacy.payroll_retention_years'", c=cid)
        sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, 'privacy.payroll_retention_years', 'TENANT', "
            "NULL, CAST(:v AS jsonb), false, '2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=cid, v=str(years))

    emma = w["emp"]["Emma"]["id"]
    set_years(1)
    run_id, rows = emma_run(w)
    generate(cid, run_id)
    doc_id = payslip_of(rows[emma])[0]
    issue_date, until = sql("SELECT issue_date, retention_until FROM documents WHERE id = :d", d=doc_id)[0]
    assert until == add_years(issue_date, 8)
    set_years(10)
    later, later_rows = emma_run(w, start=date(2026, 10, 1))
    generate(cid, later)
    other = payslip_of(later_rows[emma])[0]
    issue_date2, until2 = sql("SELECT issue_date, retention_until FROM documents WHERE id = :d", d=other)[0]
    assert until2 == add_years(issue_date2, 10)
