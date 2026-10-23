"""Integration gap-fill: endpoints the payroll v2 frontend reported missing.

* GET /payroll/runs?needs_my_action=true (the run's next step, scoped, approval steps for the current approver);
* GET /me/payroll/preferences (settings resolved for the caller's own employee, next pay date);
* payslip questions (payslip.queries_enabled; payslip_queries with RLS and composite keys);
* GET /me/payroll/loan-policy and GET /payroll/employees/{id}/loan-policy;
* GET /payroll/manual-settlements/{id}/file (manual payment proof for reconcilers in scope);
* GET /payroll/funding-accounts/options (masked accounts for payment users);
* GET /payroll/downloads/mine and POST /payroll/report-exports/{id}/link (signed links for report files too);
* payslip lines carry the stored explanation of EMPLOYEE_VISIBLE lines only.

Every endpoint is checked positive and negative, by scope, and across three tenants (EC-SEC-01, EC-SEC-02,
EC-SEC-05, EC-DOC-01, EC-DOC-06).
"""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payment_obligations import (  # noqa: F401  (pay is a fixture)
    INSTRUCTIONS,
    add_funding_account,
    finalized_run as paid_run,
    instructions_of,
    pay,
    submitted_batch,
)
from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    ME,
    PAY,
    docs_world,
    emma_run,
    finalized_run,
    generate,
    payslip_of,
    period,
)
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    RUNS,
    current_period,
    force_status,
    new_run,
    runs_world,
)
from tests.payroll.test_wage_inputs import (
    CHECK,
    DENIED,
    FOREIGN_KEY,
    activate_policy,
    audit_actions,
    expect_error,
    place,
    role,
    session,
    set_setting,
    sql,
    tenant,
    today_of,
    user_with_role,
)

QUESTIONS = f"{PAY}/payslip-questions"
MY_QUESTIONS = f"{ME}/payslip-questions"
CENTER = f"{PAY}/downloads/mine"
OPTIONS = f"{PAY}/funding-accounts/options"
PDF = b"%PDF-1.4\n% proof of a cheque payment\n"


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def err(response, status, code=None):
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json().get("code") == code, response.text
    return response.json()


def ask(client, document_id, question="Why is my PF deduction 1,800.00 this month?"):
    return client.post(f"{ME}/payslips/{document_id}/questions", json={"question": question})


def emma_payslip(w, start=date(2026, 9, 1), **kwargs):
    run_id, rows = emma_run(w, start, **kwargs)
    generate(w["cid"], run_id)
    return run_id, {name: payslip_of(rows[w["emp"][name]["id"]])[0] for name in ("Emma", "Ivan")}


def tenant_payslip(t, start=date(2026, 9, 1)):
    run_id, rows = finalized_run(t["cid"], t["entity"], t["group"], start, [t["employee"]["id"]])
    generate(t["cid"], run_id)
    return payslip_of(rows[t["employee"]["id"]])[0]


def unlinked_user(app, admin, name="Noah"):
    """A signed-in user whose employee record has been unlinked: the principal has no employee."""
    employee = create_employee(admin, name, hire_date="2024-01-15")
    client = login_as(app, admin, employee, ["employee"])
    sql("UPDATE employees SET user_id = NULL WHERE id = :e", e=employee["id"])
    return client


# ---------------------------------------------------------------------------
# 8. Employee-safe line explanations on payslips
# ---------------------------------------------------------------------------
EXPLAINED_LINES = (
    ("BASIC", "EARNING", "50000.00", {"explanation": "Basic pay 50,000.00 for 30 of 30 paid days"}),
    ("HRA", "EARNING", "20000.00", {}),
    ("PF_EMPLOYEE", "DEDUCTION", "1800.00", {"statutory": True, "explanation": "12% of basic, capped at 15,000.00"}),
    ("RETENTION_ACCRUAL", "EARNING", "999.00",
     {"visibility": "HR_ONLY", "explanation": "Confidential retention accrual approved by the board"}),
)


def test_payslip_lines_carry_the_stored_explanation_of_employee_visible_lines_only(docs_world):
    w = docs_world
    _, docs = emma_payslip(w, lines=EXPLAINED_LINES)
    body = ok(w["logins"]["Emma"].get(f"{ME}/payslips/{docs['Emma']}"))
    content = body["content"]
    assert [(i["code"], i["explanation"]) for i in content["earnings"]] == [
        ("BASIC", "Basic pay 50,000.00 for 30 of 30 paid days"), ("HRA", None)]
    assert content["deductions"][0]["explanation"] == "12% of basic, capped at 15,000.00"
    dumped = json.dumps(body)
    assert "Confidential retention" not in dumped and "RETENTION_ACCRUAL" not in dumped  # never HR_ONLY lines
    assert all(set(i) & {"trace", "sources", "tags"} == set() for i in content["earnings"] + content["deductions"])
    # Another employee's payslip stays 404 (EC-DOC-01); staff see the same explanation through their own route.
    err(w["logins"]["Ivan"].get(f"{ME}/payslips/{docs['Emma']}"), 404)
    staff = ok(w["acme"].get(f"{PAY}/payslips/{docs['Emma']}"))
    assert staff["content"]["earnings"][0]["explanation"].startswith("Basic pay")
    err(w["beta"]["client"].get(f"{PAY}/payslips/{docs['Emma']}"), 404)


# ---------------------------------------------------------------------------
# 3. Payslip questions
# ---------------------------------------------------------------------------
def test_payslip_questions_employee_asks_and_staff_in_scope_answers(docs_world):
    w = docs_world
    cid, emma, ivan, cleo, hr = w["cid"], w["logins"]["Emma"], w["logins"]["Ivan"], w["clerk"], w["acme"]
    emma_id = w["emp"]["Emma"]["id"]
    set_setting(cid, "payslip.queries_enabled", True)
    _, docs = emma_payslip(w)
    url = f"{ME}/payslips/{docs['Emma']}/questions"
    err(emma.post(url, json={"question": "   "}), 422)
    err(emma.post(url, json={"question": "x" * 2001}), 422)
    err(emma.post(url, json={"question": "Why?", "employee_id": 1}), 422)  # strict body
    err(ask(ivan, docs["Emma"]), 404)  # another employee's payslip (EC-DOC-01)
    err(ask(emma, 99999999), 404)
    asked = ok(ask(emma, docs["Emma"]), 201)
    assert asked["status"] == "OPEN" and asked["document_id"] == docs["Emma"]
    qid = asked["id"]
    assert "PAYSLIP_QUESTION_ASKED" in audit_actions(cid, "payslip_query", qid)

    mine = ok(emma.get(MY_QUESTIONS))
    assert mine["total"] == 1 and mine["items"][0]["question"].startswith("Why is my PF")
    assert mine["items"][0]["payslip"]["id"] == docs["Emma"]
    assert "asked_by_user_id" not in mine["items"][0] and "employee" not in mine["items"][0]
    assert ok(ivan.get(MY_QUESTIONS)) == {"total": 0, "items": []}
    err(ivan.get(f"{MY_QUESTIONS}/{qid}"), 404)
    err(ivan.post(f"{MY_QUESTIONS}/{qid}/close"), 404)

    # Employees have no staff scope; the AE01 clerk (payslip read/publish on AE01 only) sees nothing of LE01.
    err(emma.get(QUESTIONS), 403)
    err(emma.post(f"{QUESTIONS}/{qid}/answer", json={"answer": "Self answer"}), 403)
    assert ok(cleo.get(QUESTIONS))["total"] == 0
    err(cleo.get(f"{QUESTIONS}/{qid}"), 404)
    err(cleo.post(f"{QUESTIONS}/{qid}/answer", json={"answer": "Out of scope"}), 404)

    staff = ok(hr.get(QUESTIONS, params={"status": "OPEN"}))
    assert staff["total"] == 1 and staff["items"][0]["employee"]["id"] == emma_id
    assert ok(hr.get(QUESTIONS, params={"status": "ANSWERED"}))["total"] == 0
    assert ok(hr.get(QUESTIONS, params={"employee_id": w["emp"]["Ivan"]["id"]}))["total"] == 0
    err(hr.post(f"{QUESTIONS}/{qid}/answer", json={"answer": "Stale", "row_version": 99}), 409, "ROW_VERSION_CONFLICT")
    err(hr.post(f"{QUESTIONS}/{qid}/answer", json={"answer": " "}), 422)
    answered = ok(hr.post(f"{QUESTIONS}/{qid}/answer",
                          json={"answer": "PF is 12% of basic pay, capped.", "row_version": asked["row_version"]}))
    assert answered["status"] == "ANSWERED"
    err(hr.post(f"{QUESTIONS}/{qid}/answer", json={"answer": "Again"}), 409, "QUESTION_NOT_OPEN")
    assert "PAYSLIP_QUESTION_ANSWERED" in audit_actions(cid, "payslip_query", qid)
    title, message, action = sql("SELECT title, message, action_url FROM notifications WHERE user_id = :u "
                                 "ORDER BY id DESC LIMIT 1", u=emma.user_id)[0]
    assert title == "Your payslip question was answered" and action == f"/me/payslips?modal=payslip-question&id={qid}"
    assert "12%" not in message  # the notification never carries the answer or amounts

    seen = ok(emma.get(f"{MY_QUESTIONS}/{qid}"))
    assert seen["answer"] == "PF is 12% of basic pay, capped." and "answered_by_user_id" not in seen
    closed = ok(emma.post(f"{MY_QUESTIONS}/{qid}/close", json={"row_version": seen["row_version"]}))
    assert closed["status"] == "CLOSED"
    err(emma.post(f"{MY_QUESTIONS}/{qid}/close"), 409, "QUESTION_CLOSED")
    assert "PAYSLIP_QUESTION_CLOSED" in audit_actions(cid, "payslip_query", qid)

    # The setting decides (resolved for the employee, never assumed).
    set_setting(cid, "payslip.queries_enabled", False)
    err(ask(emma, docs["Emma"]), 409, "QUERIES_DISABLED")
    set_setting(cid, "payslip.queries_enabled", True)
    for n in range(5):
        ok(ask(emma, docs["Emma"], f"Question {n}"), 201)
    err(ask(emma, docs["Emma"]), 409, "TOO_MANY_OPEN_QUESTIONS")
    assert ok(emma.get(MY_QUESTIONS, params={"status": "OPEN"}))["total"] == 5


def test_nobody_answers_a_question_about_their_own_payslip(app, docs_world):
    w = docs_world
    desk = role(w["acme"], "Payslip desk", "COMPANY", ("payroll:payslip:read", "payroll:payslip:publish"))
    hana_employee = create_employee(w["acme"], "Hana", hire_date="2024-01-15")
    place(hana_employee["id"], w["le01"], w["group"])
    hana = user_with_role(app, w["acme"], hana_employee, desk)
    run_id, rows = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 8, 1), [hana_employee["id"]])
    generate(w["cid"], run_id)
    question = ok(ask(hana, payslip_of(rows[hana_employee["id"]])[0]), 201)
    err(hana.post(f"{QUESTIONS}/{question['id']}/answer", json={"answer": "Fine"}), 403, "SELF_ANSWER")
    with tenant(w["cid"]) as conn:  # the database refuses it as well
        expect_error(conn, CHECK, "UPDATE payslip_queries SET answer = 'x', answered_at = now(), status = 'ANSWERED', "
                     "answered_by_user_id = asked_by_user_id WHERE id = :q", q=question["id"])
    assert ok(w["acme"].post(f"{QUESTIONS}/{question['id']}/answer", json={"answer": "Checked"}))["status"] == "ANSWERED"


def test_ec_sec_01_payslip_queries_are_isolated_across_three_tenants(docs_world):
    w = docs_world
    _, docs = emma_payslip(w)
    seeded = {w["cid"]: {"query": ok(ask(w["logins"]["Emma"], docs["Emma"]), 201)["id"], "document": docs["Emma"],
                         "employee": w["emp"]["Emma"]["id"], "user": w["logins"]["Emma"].user_id,
                         "entity": w["le01"]}}
    for key in ("beta", "cobalt"):
        t = w[key]
        document = tenant_payslip(t)
        err(ask(w["logins"]["Emma"], document), 404)  # another tenant's payslip id (EC-SEC-02)
        query = ok(ask(t["login"], document), 201)["id"]
        seeded[t["cid"]] = {"query": query, "document": document, "employee": t["employee"]["id"],
                            "user": t["login"].user_id, "entity": t["entity"]}
        err(w["acme"].get(f"{QUESTIONS}/{query}"), 404)
        err(w["acme"].post(f"{QUESTIONS}/{query}/answer", json={"answer": "Not yours"}), 404)
        err(w["logins"]["Emma"].get(f"{MY_QUESTIONS}/{query}"), 404)
        err(w["logins"]["Emma"].post(f"{MY_QUESTIONS}/{query}/close"), 404)
        listed = ok(t["client"].get(QUESTIONS))
        assert [i["id"] for i in listed["items"]] == [query] and listed["total"] == 1
        assert [i["id"] for i in ok(t["login"].get(MY_QUESTIONS))["items"]] == [query]
    assert {i["id"] for i in ok(w["acme"].get(QUESTIONS))["items"]} == {seeded[w["cid"]]["query"]}
    companies = list(seeded)
    with tenant(None) as conn:
        assert conn.execute(text("SELECT count(*) FROM payslip_queries")).scalar() == 0
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            assert {o for (o,) in conn.execute(text("SELECT company_id FROM payslip_queries"))} == {cid}
            for other in others:
                foreign = seeded[other]["query"]
                assert conn.execute(text("SELECT count(*) FROM payslip_queries WHERE id = :i"),
                                    {"i": foreign}).scalar() == 0
                assert conn.execute(text("UPDATE payslip_queries SET question = 'x' WHERE id = :i"),
                                    {"i": foreign}).rowcount == 0
            expect_error(conn, DENIED, "UPDATE payslip_queries SET company_id = :o WHERE id = :i", o=others[0],
                         i=seeded[cid]["query"])
    a, b = companies[0], companies[1]
    own, foreign = seeded[a], seeded[b]
    insert = ("INSERT INTO payslip_queries (company_id, document_id, employee_id, legal_entity_id, question, status, "
              "asked_by_user_id, row_version, created_at, updated_at) VALUES (:c, :d, :e, :le, 'Attack', 'OPEN', :u, "
              "1, now(), now())")
    with tenant(a) as conn:  # composite keys refuse every reference to another tenant's rows
        expect_error(conn, FOREIGN_KEY, insert, c=a, d=foreign["document"], e=own["employee"], le=own["entity"],
                     u=own["user"])
        expect_error(conn, FOREIGN_KEY, insert, c=a, d=own["document"], e=foreign["employee"], le=own["entity"],
                     u=own["user"])
        expect_error(conn, FOREIGN_KEY, insert, c=a, d=own["document"], e=own["employee"], le=foreign["entity"],
                     u=own["user"])
        expect_error(conn, FOREIGN_KEY, insert, c=a, d=own["document"], e=own["employee"], le=own["entity"],
                     u=foreign["user"])
        expect_error(conn, CHECK, insert.replace("'Attack'", "repeat('a', 2001)"), c=a, d=own["document"],
                     e=own["employee"], le=own["entity"], u=own["user"])


# ---------------------------------------------------------------------------
# 2. Self-service preferences
# ---------------------------------------------------------------------------
def test_my_payroll_preferences_are_resolved_for_the_callers_own_employee(app, docs_world):
    w = docs_world
    cid, emma = w["cid"], w["logins"]["Emma"]
    for key, value in (("payslip.queries_enabled", True), ("payslip.pdf_protection", "NONE"),
                       ("payroll.bank.split_payments_enabled", False), ("payroll.bank.max_accounts_per_payee", 2)):
        set_setting(cid, key, value)
    today = today_of(cid)
    this_month = today.replace(day=1)
    previous = (this_month - timedelta(days=1)).replace(day=1)
    upcoming = (this_month + timedelta(days=40)).replace(day=1)
    later = (upcoming + timedelta(days=40)).replace(day=1)
    for start in (previous, later, upcoming):
        period(cid, w["group"], start)
    expected = (later - timedelta(days=1)).isoformat()  # the pay date (period end) of the upcoming period

    prefs = ok(emma.get(f"{ME}/preferences"))
    assert prefs["employee_id"] == w["emp"]["Emma"]["id"]
    assert prefs["payslip"] == {"queries_enabled": True, "pdf_protection": "NONE"}
    group = prefs["pay_group"]
    assert (group["id"], group["currency"], group["next_pay_date"]) == (w["group"], "INR", expected)
    assert group["next_period"]["start"] == upcoming.isoformat()
    bank = prefs["bank_accounts"]
    assert (bank["split_payments_enabled"], bank["max_accounts_per_payee"], bank["effective_max_accounts"]) == (
        False, 2, 1)
    assert bank["allowed_countries"] is None and isinstance(bank["verification_methods"], list)
    assert prefs["legal_entity"]["id"] == w["le01"] and prefs["unavailable"] == []
    assert "secret" not in json.dumps(prefs).lower() and "password" not in json.dumps(prefs).lower()

    # Values follow the settings for this employee, never assumed defaults.
    for key, value in (("payslip.queries_enabled", False), ("payslip.pdf_protection", "EMPLOYEE_SECRET"),
                       ("payroll.bank.split_payments_enabled", True), ("payroll.bank.max_accounts_per_payee", 3)):
        set_setting(cid, key, value)
    changed = ok(emma.get(f"{ME}/preferences"))
    assert changed["payslip"] == {"queries_enabled": False, "pdf_protection": "EMPLOYEE_SECRET"}
    assert (changed["bank_accounts"]["split_payments_enabled"], changed["bank_accounts"]["effective_max_accounts"]) == (
        True, 3)

    # Own data only: Ivan gets his own record; other tenants' employees get theirs; no employee -> 404.
    assert ok(w["logins"]["Ivan"].get(f"{ME}/preferences"))["employee_id"] == w["emp"]["Ivan"]["id"]
    for key in ("beta", "cobalt"):
        t = w[key]
        theirs = ok(t["login"].get(f"{ME}/preferences"))
        assert theirs["employee_id"] == t["employee"]["id"] and theirs["pay_group"]["id"] == t["group"]
        assert theirs["pay_group"]["next_pay_date"] is None  # no stored periods: null, never a guess
    err(unlinked_user(app, w["acme"]).get(f"{ME}/preferences"), 404)


# ---------------------------------------------------------------------------
# 4. Loan policy preview
# ---------------------------------------------------------------------------
LOAN_POLICY = {"max_amount_formula": "3 * MONTHLY_GROSS", "max_tenure_months": 12, "interest_rate_percent": "12",
               "interest_method": "REDUCING", "max_instalment_percent_of_net": "40", "min_service_months": 6,
               "max_active_loans": 1}
ADVANCE_POLICY = {"max_percent_of_monthly_net": "50", "recover_in_runs": 2, "max_per_year": 1}


def test_loan_policy_preview_shows_the_resolved_limits_and_own_usage_only(app, docs_world):
    from app.payroll.services import loans

    w = docs_world
    cid, emma, emma_id = w["cid"], w["logins"]["Emma"], w["emp"]["Emma"]["id"]
    set_setting(cid, "payroll.loans.enabled", False)
    set_setting(cid, "payroll.advances.enabled", False)
    off = ok(emma.get(f"{ME}/loan-policy"))
    assert off["employee_id"] == emma_id and off["currency"] == "INR"
    assert off["loan"]["enabled"] is False and off["advance"]["enabled"] is False

    set_setting(cid, "payroll.loans.enabled", True)
    set_setting(cid, "payroll.advances.enabled", True)
    set_setting(cid, "payroll.deductions.min_net_pay_percent", "50")
    activate_policy(w["acme"], "LOAN_POLICY", LOAN_POLICY)
    activate_policy(w["acme"], "ADVANCE_POLICY", ADVANCE_POLICY)
    previous = loans.set_pay_figures_provider(
        lambda db, c, e, on, currency: loans.PayFigures(Decimal("50000"), Decimal("40000")))
    try:
        body = ok(emma.get(f"{ME}/loan-policy"))
        hr_view = ok(w["acme"].get(f"{PAY}/employees/{emma_id}/loan-policy"))
    finally:
        loans.set_pay_figures_provider(previous)
    loan, advance = body["loan"], body["advance"]
    assert loan["enabled"] is True and loan["policy_status"] == "RESOLVED"
    limits = loan["limits"]
    assert limits["max_amount_formula"] == "3 * MONTHLY_GROSS"
    assert limits["max_amount"] == {"amount": "150000.00", "currency": "INR"}
    assert (limits["max_tenure_months"], limits["interest_method"], limits["max_active_loans"]) == (12, "REDUCING", 1)
    assert Decimal(limits["interest_rate_percent"]) == 12 and Decimal(limits["max_instalment_percent_of_net"]) == 40
    assert loan["usage"]["open"] == 0 and loan["usage"]["service_months"] >= 12
    assert advance["limits"]["max_amount"] == {"amount": "20000.00", "currency": "INR"}
    assert (advance["limits"]["recover_in_runs"], advance["limits"]["max_per_year"]) == (2, 1)
    assert Decimal(body["protected_net_percent"]) == 50
    dumped = json.dumps(body)
    assert "approval_chain_id" not in dumped and "criteria" not in dumped  # no chains or assignment rules
    assert hr_view["loan"]["limits"] == limits and hr_view["employee_id"] == emma_id

    # Scope: staff route needs payroll:loans:read over the employee; SELF holders and other tenants get 404.
    err(w["clerk"].get(f"{PAY}/employees/{emma_id}/loan-policy"), 403)
    err(w["logins"]["Ivan"].get(f"{PAY}/employees/{emma_id}/loan-policy"), 404)
    err(emma.get(f"{PAY}/employees/{emma_id}/loan-policy"), 404)
    for key in ("beta", "cobalt"):
        t = w[key]
        err(t["client"].get(f"{PAY}/employees/{emma_id}/loan-policy"), 404)
        assert ok(t["login"].get(f"{ME}/loan-policy"))["employee_id"] == t["employee"]["id"]
    err(unlinked_user(app, w["acme"]).get(f"{ME}/loan-policy"), 404)


# ---------------------------------------------------------------------------
# 6. Funding account options for payment users
# ---------------------------------------------------------------------------
def test_funding_account_options_are_masked_and_scoped_for_payment_users(app, docs_world):
    w = docs_world
    cid, acme = w["cid"], w["acme"]
    preparer_role = role(acme, "AE payment preparer", "LEGAL_ENTITY", ("payroll:payment:prepare",))
    reconciler_role = role(acme, "Reconciler", "COMPANY", ("payroll:payment:reconcile",))
    preparer = user_with_role(app, acme, create_employee(acme, "Pat", hire_date="2024-01-15"), preparer_role, [w["ae"]])
    reconciler = user_with_role(app, acme, create_employee(acme, "Rita", hire_date="2024-01-15"), reconciler_role)
    india = add_funding_account(cid, w["le01"], [w["clerk"].user_id], number="912020012345678")
    gulf = add_funding_account(cid, w["ae"], [reconciler.user_id], number="912020099998888", name="Gulf payroll")
    old = add_funding_account(cid, w["le01"], [w["clerk"].user_id], number="912020011112222", name="Old account")
    sql("UPDATE funding_accounts SET status = 'INACTIVE', status_reason = 'Closed' WHERE id = :f", f=old)

    items = ok(preparer.get(OPTIONS))["items"]
    assert [i["id"] for i in items] == [gulf]  # AE01 only
    item = items[0]
    assert item["account_masked"].endswith("8888") and item["legal_entity"]["id"] == w["ae"]
    assert (item["currency"], item["active"], item["is_signatory"]) == ("INR", True, False)
    dumped = json.dumps(items)
    assert "912020099998888" not in dumped and "99998888" not in dumped
    assert not {"signatory_user_ids", "routing", "holder_name", "provider_profile"} & set(item)
    assert ok(preparer.get(OPTIONS, params={"legal_entity_id": w["le01"]}))["items"] == []  # never reveals LE01
    err(preparer.get(f"{PAY}/funding-accounts"), 403)  # the full list still needs manage
    err(preparer.get(f"{PAY}/funding-accounts/{gulf}"), 403)

    listed = {i["id"]: i for i in ok(reconciler.get(OPTIONS))["items"]}
    assert set(listed) == {india, gulf} and listed[gulf]["is_signatory"] is True
    assert {i["id"] for i in ok(reconciler.get(OPTIONS, params={"include_inactive": "true"}))["items"]} == {
        india, gulf, old}
    assert ok(reconciler.get(OPTIONS, params={"currency": "AED"}))["items"] == []
    err(reconciler.get(OPTIONS, params={"currency": "inr"}), 422)
    err(w["logins"]["Emma"].get(OPTIONS), 403)
    err(w["clerk"].get(OPTIONS), 403)  # payslip clerk: no payment permission

    for key in ("beta", "cobalt"):
        t = w[key]
        theirs = add_funding_account(t["cid"], t["entity"], [t["login"].user_id], number="912020055556666")
        payer = login_as(app, t["client"], create_employee(t["client"], "Fin", hire_date="2024-01-15"),
                         ["finance_payments"])
        assert [i["id"] for i in ok(payer.get(OPTIONS))["items"]] == [theirs]
        assert theirs not in {i["id"] for i in ok(reconciler.get(OPTIONS, params={"include_inactive": "true"}))["items"]}


# ---------------------------------------------------------------------------
# 7. Download center and signed report links
# ---------------------------------------------------------------------------
def test_download_center_merges_own_exports_and_report_exports_with_signed_links(docs_world):
    w = docs_world
    cid, acme = w["cid"], w["acme"]
    run_id, _ = emma_payslip(w)
    export = ok(acme.post(f"{PAY}/exports", json={"kind": "REGISTER_CSV", "run_id": run_id}), 202)
    report = ok(acme.post(f"{PAY}/reports/payroll_register/exports", json={"format": "CSV"}), 202)

    center = ok(acme.get(CENTER))
    assert center["total"] == 2 and [i["source"] for i in center["items"]] == ["REPORT_EXPORT", "EXPORT"]
    by_source = {i["source"]: i for i in center["items"]}
    assert by_source["EXPORT"]["id"] == export["id"] and by_source["REPORT_EXPORT"]["id"] == report["id"]
    for item in center["items"]:
        assert item["status"] == "READY" and item["download"]["expires_in_seconds"] == 300
        served = acme.get(item["download"]["url"])
        assert served.status_code == 200 and served.content, served.text
        assert "no-store" in served.headers["cache-control"]
    first, second = (ok(acme.get(CENTER, params={"limit": 1, "skip": skip})) for skip in (0, 1))
    assert first["total"] == second["total"] == 2
    assert [first["items"][0]["id"], second["items"][0]["id"]] == [report["id"], export["id"]]
    assert [i["id"] for i in ok(acme.get(CENTER, params={"source": "REPORT_EXPORT"}))["items"]] == [report["id"]]
    err(acme.get(CENTER, params={"source": "ANYTHING"}), 422)
    listed = ok(acme.get(f"{PAY}/report-exports", params={"skip": 0, "limit": 1}))
    assert listed["total"] == 1 and [i["id"] for i in listed["items"]] == [report["id"]]
    assert ok(acme.get(f"{PAY}/report-exports", params={"skip": 1}))["items"] == []

    link = ok(acme.post(f"{PAY}/report-exports/{report['id']}/link"))
    assert link["url"].startswith(f"{PAY}/downloads/") and link["expires_in_seconds"] == 300
    signed = acme.get(link["url"])
    direct = acme.get(f"{PAY}/report-exports/{report['id']}/download")
    assert signed.status_code == 200 and signed.content == direct.content
    assert audit_actions(cid, "report_export", report["id"]).count("REPORT_EXPORT_DOWNLOADED") >= 2
    token = link["url"].rsplit("/", 1)[1]
    err(acme.get(f"{PAY}/downloads/{token[:-2]}AA"), 404)  # tampered
    err(acme.post(f"{PAY}/report-exports/99999999/link"), 404)

    # Only the requester: another user of the tenant and other tenants see nothing and cannot redeem or mint.
    for other in (w["clerk"], w["beta"]["client"], w["cobalt"]["client"]):
        assert ok(other.get(CENTER))["total"] == 0
        err(other.get(link["url"]), 404)
        err(other.get(by_source["EXPORT"]["download"]["url"]), 404)
        err(other.post(f"{PAY}/report-exports/{report['id']}/link"), 404)
    assert ok(w["logins"]["Emma"].get(CENTER)) == {"total": 0, "skip": 0, "limit": 50, "items": []}
    err(w["logins"]["Emma"].post(f"{PAY}/report-exports/{report['id']}/link"), 403)  # no export permission


# ---------------------------------------------------------------------------
# 5. Manual payment proof file
# ---------------------------------------------------------------------------
def test_manual_proof_file_is_served_to_reconcilers_in_scope_only(app, pay):
    cid, admin = pay["cid"], pay["admin"]
    run_id = paid_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    instruction_id = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    proof = ok(pay["fran"].post(f"{INSTRUCTIONS}/{instruction_id}/manual-settle",
                                data={"method": "CHEQUE", "reference": "CHQ 000777"},
                                files={"file": ("cheque scan.pdf", PDF, "application/pdf")}), 201)
    url = f"{PAY}/manual-settlements/{proof['id']}/file"

    shown = pay["pete"].get(url)  # the second reviewer views it before deciding
    assert shown.status_code == 200 and shown.content == PDF, shown.text
    assert shown.headers["content-type"].startswith("application/pdf")
    assert shown.headers["content-disposition"].startswith("inline") and "no-store" in shown.headers["cache-control"]
    assert shown.headers["x-proof-sha256"] == hashlib.sha256(PDF).hexdigest()
    saved = pay["pete"].get(url, params={"download": "true"})
    assert saved.status_code == 200 and saved.headers["content-disposition"].startswith('attachment; filename="cheque')
    assert audit_actions(cid, "payment_manual_settlement", proof["id"]).count("MANUAL_PROOF_DOWNLOADED") == 2
    ok(pay["pete"].post(f"{PAY}/manual-settlements/{proof['id']}/approve", json={"note": "Cheque cleared"}))
    assert pay["pete"].get(url).status_code == 200  # still viewable after the review

    processor = login_as(app, admin, create_employee(admin, "Pia"), ["payroll_processor"])
    err(processor.get(url), 403)  # no reconcile permission
    emma = login_as(app, admin, pay["emp"]["Emma"], ["employee"])
    err(emma.get(url), 403)  # the payee never reads the evidence file
    err(pay["pete"].get(f"{PAY}/manual-settlements/99999999/file"), 404)
    r = admin.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                    "country_code": "AE", "base_currency": "AED",
                                                    "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    gulf_reconciler = user_with_role(app, admin, create_employee(admin, "Rex"),
                                     role(admin, "AE reconciler", "LEGAL_ENTITY", ("payroll:payment:reconcile",)),
                                     [r.json()["id"]])
    err(gulf_reconciler.get(url), 404)  # outside the entity scope
    for name in ("Beta Pay", "Cobalt Pay"):
        other = signup_company(app, name)
        finance = login_as(app, other, create_employee(other, "Fin"), ["finance_payments"])
        err(finance.get(url), 404)  # another tenant (EC-SEC-02)


# ---------------------------------------------------------------------------
# 1. Pay runs that need the caller's action
# ---------------------------------------------------------------------------
def _needing(client, **params):
    body = ok(client.get(RUNS, params={"needs_my_action": "true", "limit": 200, **params}))
    assert body["total"] == len(body["items"])
    return {i["id"] for i in body["items"]}


def test_runs_needs_my_action_follows_the_next_step_scope_and_current_approver(app, runs_world):
    from app.payroll.services import approvals

    w = runs_world
    acme, processor, approver, cleo = w["acme"], w["processor"], w["approver"], w["cleo"]
    run = new_run(acme, w["group"], w["period"]["id"])["id"]
    ae_run = new_run(acme, w["ae_group"], current_period(acme, w["ae_group"])["id"])["id"]
    # DRAFT: the pre-check is the step of run creators in scope.
    assert {run, ae_run} <= _needing(acme)
    assert _needing(processor) == {run, ae_run}
    assert _needing(cleo) == {ae_run}  # AE01 only
    assert _needing(approver) == set()  # nothing to approve yet, though the approver reads both runs
    assert {run, ae_run} <= {i["id"] for i in ok(approver.get(RUNS))["items"]}

    force_status(run, "APPROVED")  # finalize: payroll:run:finalize (the manager), not the processor
    assert run in _needing(acme) and run not in _needing(processor) and run not in _needing(approver)

    force_status(run, "APPROVAL_PENDING")
    assert run not in _needing(approver)  # no approval request routes it to them
    with session(w["cid"]) as (db, _):
        approvals.submit(db, None, module="payroll_run", subject_type="payroll_run", subject_id=run,
                         context={"legal_entity_id": w["le01"], "pay_group_id": w["group"]})
        db.commit()
    assert run in _needing(approver)  # the current approver
    assert run not in _needing(processor) and run not in _needing(cleo)
    assert _needing(approver, status="DRAFT") == set()  # other filters still apply
    paged = ok(acme.get(RUNS, params={"needs_my_action": "true", "limit": 1}))
    assert len(paged["items"]) == 1 and paged["total"] == len(_needing(acme))

    for key in ("beta", "cobalt"):  # other tenants never see these runs
        client = w[key]["client"]
        assert not {run, ae_run} & _needing(client)
        err(client.get(f"{RUNS}/{run}"), 404)
    employee = login_as(app, acme, w["emp"]["Emma"], ["employee"])
    err(employee.get(RUNS, params={"needs_my_action": "true"}), 403)  # no payroll:run:read
