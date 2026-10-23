"""Adversarial review of the integration gap-fill (permissions and honesty).

* The "question answered" notification deep-links to the employee's own question on My Payslips
  (``/me/payslips?modal=payslip-question&id=``), a page that exists, and carries no text or amounts.
* Signed export links (payslip ZIP / register) re-check the requester's permission and scope when they are
  minted and redeemed (EC-DOC-06): a narrowed scope is 404, a revoked permission 403, and the download
  center stops offering a link to someone who can no longer export.
* A manual payment proof uploaded under a non-Latin file name is still served (the header name is ASCII).
* ``GET /me/payroll/loan-policy`` without any loans access is 403 before anything about the caller's
  placement is looked up.
* Report exports get the same EC-DOC-06 re-check as other exports when a link is minted, redeemed or the
  file is downloaded directly: a narrowed scope is 404 (a widened one still works), a revoked report
  permission (reports:export, or a report's own extra permission such as payroll:audit:read) is 403, an
  unmasked file needs the company-wide export right it was built with, and the download center stops
  offering a link the server would refuse.
* Answering a question whose employee no longer has a login still works (no notification, no error).
"""

from tests.helpers import create_employee
from tests.payroll.test_integration_gapfill import (  # noqa: F401  (docs_world, pay are fixtures)
    CENTER,
    MY_QUESTIONS,
    PDF,
    QUESTIONS,
    ask,
    docs_world,
    emma_payslip,
    err,
    ok,
    pay,
)
from tests.payroll.test_payment_obligations import INSTRUCTIONS, instructions_of, submitted_batch
from tests.payroll.test_payment_obligations import finalized_run as paid_run
from tests.payroll.test_payslips_v2 import PAY
from tests.payroll.test_wage_inputs import role, set_setting, sql, user_with_role


def test_answered_notification_deep_links_to_the_employees_own_question(docs_world):
    w = docs_world
    emma, ivan, hr = w["logins"]["Emma"], w["logins"]["Ivan"], w["acme"]
    set_setting(w["cid"], "payslip.queries_enabled", True)
    _, docs = emma_payslip(w)
    question = ok(ask(emma, docs["Emma"], "Why is my PF deduction 1,800.00?"), 201)
    ok(hr.post(f"{QUESTIONS}/{question['id']}/answer", json={"answer": "PF is 12% of basic pay, 1,800.00."}))
    [(action, message)] = sql("SELECT action_url, message FROM notifications WHERE user_id = :u "
                              "AND title = 'Your payslip question was answered'", u=emma.user_id)
    # The frontend has /me/payslips (with the ?modal=payslip-question&id= detail), not /payslips/{id}.
    assert action == f"/me/payslips?modal=payslip-question&id={question['id']}"
    assert "1,800" not in action + message and "12%" not in action + message
    # The deep link resolves for the employee only.
    assert ok(emma.get(f"{MY_QUESTIONS}/{question['id']}"))["status"] == "ANSWERED"
    err(ivan.get(f"{MY_QUESTIONS}/{question['id']}"), 404)
    err(w["beta"]["login"].get(f"{MY_QUESTIONS}/{question['id']}"), 404)


def test_export_links_recheck_permission_and_scope_when_minted_and_redeemed(app, docs_world):
    w = docs_world
    acme = w["acme"]
    exporter_role = role(acme, "LE01 register exporter", "LEGAL_ENTITY", ("payroll:reports:export",))
    exporter = user_with_role(app, acme, create_employee(acme, "Xena", hire_date="2024-01-15"), exporter_role,
                              [w["le01"]])
    run_id, _ = emma_payslip(w)
    export = ok(exporter.post(f"{PAY}/exports", json={"kind": "REGISTER_CSV", "run_id": run_id}), 202)
    [item] = ok(exporter.get(CENTER, params={"source": "EXPORT"}))["items"]
    assert item["id"] == export["id"] and item["status"] == "READY" and item["download"] is not None
    url = item["download"]["url"]
    assert exporter.get(url).status_code == 200  # in scope: served

    # The scope moves to AE01: the run (LE01) is no longer reachable, so neither is its register.
    r = acme.put(f"/api/v1/users/{exporter.user_id}/scope-targets",
                 json={"assignments": [{"role_id": exporter_role, "legal_entity_ids": [w["ae"]]}]})
    assert r.status_code == 200, r.text
    err(exporter.get(url), 404)
    err(exporter.post(f"{PAY}/exports/{export['id']}/download"), 404)

    # The permission is revoked altogether: 403, and the download center offers no link any more.
    r = acme.put(f"/api/v1/users/{exporter.user_id}/roles", json={"role_ids": []})
    assert r.status_code == 200, r.text
    err(exporter.get(url), 403)
    err(exporter.post(f"{PAY}/exports/{export['id']}/download"), 403)
    [item] = ok(exporter.get(CENTER, params={"source": "EXPORT"}))["items"]
    assert item["id"] == export["id"] and item["download"] is None

    # The link is bound to its requester: nobody else can redeem it.
    err(acme.get(url), 404)


def test_manual_proof_with_a_non_latin_file_name_is_served(app, pay):
    run_id = paid_run(pay, {"Emma": "100.00"})
    submitted_batch(pay, run_id)
    instruction_id = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    proof = ok(pay["fran"].post(f"{INSTRUCTIONS}/{instruction_id}/manual-settle",
                                data={"method": "CHEQUE", "reference": "CHQ 000778"},
                                files={"file": ("чек №7.pdf", PDF, "application/pdf")}), 201)
    for download, disposition in ((False, 'inline; filename="7.pdf"'), (True, 'attachment; filename="7.pdf"')):
        served = pay["pete"].get(f"{PAY}/manual-settlements/{proof['id']}/file", params={"download": download})
        assert served.status_code == 200 and served.content == PDF, served.text
        assert served.headers["content-disposition"] == disposition


def test_my_loan_policy_without_loans_access_is_403_before_any_placement_lookup(app, docs_world):
    w = docs_world
    acme = w["acme"]
    reader = role(acme, "Payslip reader only", "SELF", ("payroll:payslip:read",))
    # Not placed in any pay group: before the fix this leaked 409 NOT_PLACED instead of 403.
    uma = user_with_role(app, acme, create_employee(acme, "Uma", hire_date="2024-01-15"), reader)
    err(uma.get("/api/v1/me/payroll/loan-policy"), 403)
    # An employee with loans access (SELF) still gets their own preview.
    assert w["logins"]["Emma"].get("/api/v1/me/payroll/loan-policy").status_code == 200


# ---------------------------------------------------------------------------
# Report exports: EC-DOC-06 re-check at link, redemption and direct download
# ---------------------------------------------------------------------------
def _report_role(admin, first):
    return sql("SELECT id FROM roles WHERE company_id = :c AND name = :n", c=admin.company_id,
                       n=f"Role {first}")[0][0]


def _report_export(client, code, filters=None):
    from tests.payroll.test_reports import EXPORTS, REPORTS

    created = ok(client.post(f"{REPORTS}/{code}/exports", json={"format": "CSV", "filters": filters or {}}), 202)
    assert ok(client.get(f"{EXPORTS}/{created['id']}"))["status"] == "READY"
    return created["id"]


def _center_report(client, export_id):
    [item] = [i for i in ok(client.get(CENTER, params={"source": "REPORT_EXPORT"}))["items"] if i["id"] == export_id]
    return item


def test_report_export_links_recheck_scope_when_minted_and_redeemed(app):
    from tests.payroll.test_reports import EXPORTS, SEP_FILTER, build_world, grant_role

    w = build_world(app)
    acme = w["acme"]
    rhea = grant_role(app, acme, "Rhea", [(p, "LEGAL_ENTITY") for p in (
        "payroll:reports:read", "payroll:reports:export", "payroll:run:read")], [w["zz01"]])
    role_id = _report_role(acme, "Rhea")
    export_id = _report_export(rhea, "payroll_register", SEP_FILTER)
    item = _center_report(rhea, export_id)
    assert item["status"] == "READY" and item["download"] is not None
    url = item["download"]["url"]
    assert rhea.get(url).status_code == 200  # in scope: served
    minted = ok(rhea.post(f"{EXPORTS}/{export_id}/link"))["url"]

    # The scope moves to AE01: the ZZ01 rows in the file are no longer the caller's to see.
    ok(acme.put(f"/api/v1/users/{rhea.user_id}/scope-targets",
                json={"assignments": [{"role_id": role_id, "legal_entity_ids": [w["ae01"]]}]}))
    err(rhea.get(url), 404)
    err(rhea.get(minted), 404)
    err(rhea.post(f"{EXPORTS}/{export_id}/link"), 404)
    err(rhea.get(f"{EXPORTS}/{export_id}/download"), 404)
    assert _center_report(rhea, export_id)["download"] is None

    # A scope that grew (ZZ01 and AE01) still covers the file.
    ok(acme.put(f"/api/v1/users/{rhea.user_id}/scope-targets",
                json={"assignments": [{"role_id": role_id, "legal_entity_ids": [w["zz01"], w["ae01"]]}]}))
    assert rhea.get(url).status_code == 200
    assert _center_report(rhea, export_id)["download"] is not None

    # Export permission revoked: 403 everywhere, and the center offers no link.
    ok(acme.put(f"/api/v1/users/{rhea.user_id}/roles", json={"role_ids": []}))
    err(rhea.get(url), 403)
    err(rhea.post(f"{EXPORTS}/{export_id}/link"), 403)
    err(rhea.get(f"{EXPORTS}/{export_id}/download"), 403)
    # Nobody else redeems the link (user-bound), in this tenant or another.
    err(acme.get(url), 404)


def test_report_export_needs_the_reports_own_permission_and_the_export_right_it_was_built_with(app):
    from tests.payroll.test_reports import EXPORTS, build_world, grant_role

    w = build_world(app)
    acme = w["acme"]
    auditor = grant_role(app, acme, "Uli", [("payroll:reports:read", "COMPANY"), ("payroll:reports:export", "COMPANY"),
                                            ("payroll:audit:read", "COMPANY")])
    role_id = _report_role(acme, "Uli")
    export_id = _report_export(auditor, "bank_detail_events")
    assert ok(auditor.get(f"{EXPORTS}/{export_id}"))["masked"] is False  # company-wide export: unmasked
    url = _center_report(auditor, export_id)["download"]["url"]
    assert auditor.get(url).status_code == 200

    # payroll:audit:read (the report's own permission) is withdrawn: 403, no link.
    ok(acme.patch(f"/api/v1/roles/{role_id}", json={"grants": [
        {"permission": "payroll:reports:read", "scope": "COMPANY"},
        {"permission": "payroll:reports:export", "scope": "COMPANY"}]}))
    err(auditor.get(url), 403)
    err(auditor.get(f"{EXPORTS}/{export_id}/download"), 403)
    assert _center_report(auditor, export_id)["download"] is None

    # Audit access is back but the export right shrinks to one entity: the unmasked file is out of reach.
    ok(acme.patch(f"/api/v1/roles/{role_id}", json={"grants": [
        {"permission": "payroll:reports:read", "scope": "COMPANY"},
        {"permission": "payroll:reports:export", "scope": "LEGAL_ENTITY"},
        {"permission": "payroll:audit:read", "scope": "COMPANY"}]}))
    ok(acme.put(f"/api/v1/users/{auditor.user_id}/scope-targets",
                json={"assignments": [{"role_id": role_id, "legal_entity_ids": [w["le01"]]}]}))
    err(auditor.get(url), 404)
    err(auditor.get(f"{EXPORTS}/{export_id}/download"), 404)
    assert _center_report(auditor, export_id)["download"] is None


def test_answering_a_question_of_an_employee_without_a_login_still_works(docs_world):
    w = docs_world
    emma, hr = w["logins"]["Emma"], w["acme"]
    set_setting(w["cid"], "payslip.queries_enabled", True)
    _, docs = emma_payslip(w)
    question = ok(ask(emma, docs["Emma"]), 201)
    sql("UPDATE employees SET user_id = NULL WHERE id = :e", e=w["emp"]["Emma"]["id"])
    answered = ok(hr.post(f"{QUESTIONS}/{question['id']}/answer", json={"answer": "PF is 12% of basic pay."}))
    assert answered["status"] == "ANSWERED"
    assert sql("SELECT count(*) FROM notifications WHERE title = 'Your payslip question was answered'")[0][0] == 0
