"""Isolation of payroll documents: object access, three-tenant row-level security of every document table,
cross-tenant reference attacks and the public verification page.

EC-DOC-01 (another employee's or tenant's payslip id or file URL is 404), EC-DOC-18 (verification shows issuer,
type, date, number and masked name, never amounts), EC-SEC-01 (every new table is isolated across three
tenants; composite keys refuse foreign references), EC-SEC-02 (foreign ids are 404 on every endpoint),
EC-SEC-05 (lists and counts never reveal out-of-scope documents).
"""

from datetime import date

from sqlalchemy import text

from tests.helpers import create_employee, new_client
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    ME,
    PAY,
    add_instruction,
    docs_world,
    emma_run,
    finalized_run,
    generate,
    payslip_of,
    pdf_text,

)
from tests.payroll.test_wage_inputs import CHECK, DENIED, FOREIGN_KEY, expect_error, sql, tenant

TABLES = ("documents", "document_sequences", "export_jobs", "document_access_log", "document_generation_tasks",
          "document_notifications", "invoices", "invoice_sources", "self_billing_agreements", "document_requests",
          "payslip_pdf_secrets")
TRIGGER_GUARDED = {"documents": CHECK, "document_access_log": CHECK}
APPEND_ONLY = ("document_access_log",)


def _seed(cid, entity, group, employee_id, user_id):
    """One row in every document table for the tenant (payslips through the real generation)."""
    run_id, rows = finalized_run(cid, entity, group, date(2026, 9, 1), [employee_id])
    generate(cid, run_id)
    document_id = payslip_of(rows[employee_id])[0]
    ids = {"documents": document_id, "document_sequences": sql(
        "SELECT id FROM document_sequences WHERE company_id = :c LIMIT 1", c=cid)[0][0],
        "document_generation_tasks": sql("SELECT id FROM document_generation_tasks WHERE company_id = :c LIMIT 1",
                                         c=cid)[0][0],
        "document_notifications": sql("SELECT id FROM document_notifications WHERE company_id = :c LIMIT 1",
                                      c=cid)[0][0],
        "document_access_log": sql("SELECT id FROM document_access_log WHERE company_id = :c LIMIT 1", c=cid)[0][0]}
    with tenant(cid) as conn:
        def insert(statement, **params):
            return conn.execute(text(statement), {"c": cid, **params}).scalar_one()

        ids["export_jobs"] = insert(
            "INSERT INTO export_jobs (company_id, requested_by_user_id, kind, params, status, row_version, created_at, "
            "updated_at) VALUES (:c, :u, 'REGISTER_CSV', '{}'::jsonb, 'QUEUED', 1, now(), now()) RETURNING id",
            u=user_id)
        ids["invoices"] = insert(
            "INSERT INTO invoices (company_id, kind, mode, legal_entity_id, payee_employee_id, status, currency, seller, "
            "buyer, lines, taxes, subtotal, tax_total, total, row_version, created_at, updated_at) VALUES (:c, "
            "'INVOICE', 'INVOICE', :le, :e, 'DRAFT', 'INR', '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 0, 0, "
            "0, 1, now(), now()) RETURNING id", le=entity, e=employee_id)
        ids["invoice_sources"] = insert(
            "INSERT INTO invoice_sources (company_id, invoice_id, source_type, source_id, active, created_at, "
            "updated_at) VALUES (:c, :i, 'TASK', 1, true, now(), now()) RETURNING id", i=ids["invoices"])
        ids["self_billing_agreements"] = insert(
            "INSERT INTO self_billing_agreements (company_id, employee_id, legal_entity_id, reference, effective_from, "
            "status, created_at, updated_at) VALUES (:c, :e, :le, 'SBA', '2026-01-01', 'ACTIVE', now(), now()) "
            "RETURNING id", e=employee_id, le=entity)
        ids["document_requests"] = insert(
            "INSERT INTO document_requests (company_id, employee_id, doc_type, include_amounts, status, "
            "requested_by_user_id, row_version, created_at, updated_at) VALUES (:c, :e, 'SALARY_CERTIFICATE', false, "
            "'PENDING', :u, 1, now(), now()) RETURNING id", e=employee_id, u=user_id)
        ids["payslip_pdf_secrets"] = insert(
            "INSERT INTO payslip_pdf_secrets (company_id, employee_id, secret_enc, set_at, row_version, created_at, "
            "updated_at) VALUES (:c, :e, 'sealed', now(), 1, now(), now()) RETURNING id", e=employee_id)
    return ids, run_id


def test_ec_sec_01_every_document_table_is_isolated_across_three_tenants(docs_world):
    from app.models import TENANT_TABLES

    w = docs_world
    assert set(TABLES) <= set(TENANT_TABLES)
    tenants = {w["cid"]: (w["le01"], w["group"], w["emp"]["Emma"]["id"], w["logins"]["Emma"].user_id)}
    for key in ("beta", "cobalt"):
        t = w[key]
        tenants[t["cid"]] = (t["entity"], t["group"], t["employee"]["id"], t["login"].user_id)
    seeded = {cid: _seed(cid, *args)[0] for cid, args in tenants.items()}
    companies = list(seeded)
    with tenant(None) as conn:  # no tenant bound: nothing is visible
        assert {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in TABLES} == {t: 0 for t in TABLES}
    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in TABLES:
                owners = {o for (o,) in conn.execute(text(f"SELECT company_id FROM {table}"))}
                assert owners == {cid}, table
                for other in others:
                    foreign = seeded[other][table]
                    assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE id = :i"),
                                        {"i": foreign}).scalar() == 0, table
                    if table not in APPEND_ONLY:
                        assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                            {"i": foreign}).rowcount == 0, table
                expect_error(conn, TRIGGER_GUARDED.get(table, DENIED),
                             f"UPDATE {table} SET company_id = :o WHERE id = :i", o=others[0], i=seeded[cid][table])
    a, b = companies[0], companies[1]
    foreign_entity, foreign_employee = tenants[b][0], tenants[b][2]
    with tenant(a) as conn:  # composite keys refuse every reference to another tenant's rows
        own_entity, own_employee, own_user = tenants[a][0], tenants[a][2], tenants[a][3]
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO documents (company_id, legal_entity_id, doc_type, public_id, subject_type, subject_id, "
                     "issue_date, source_key, version, title, status, content, content_hash, sha256, bytes, file_name, "
                     "template_version, renderer, locale, created_at, updated_at) VALUES (:c, :le, 'PAYSLIP_V2', "
                     "'attackattackattackattack', 'EMPLOYEE', 1, '2026-09-30', 'x', 1, 'x', 'GENERATED', '{}'::jsonb, "
                     "repeat('a', 64), repeat('b', 64), 1, 'x.pdf', 't', 'r', 'en', now(), now())",
                     c=a, le=foreign_entity)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO documents (company_id, legal_entity_id, doc_type, public_id, subject_type, subject_id, "
                     "employee_id, run_id, issue_date, source_key, version, title, status, content, content_hash, sha256, "
                     "bytes, file_name, template_version, renderer, locale, created_at, updated_at) VALUES (:c, :le, "
                     "'PAYSLIP_V2', 'attackattackattackattac2', 'EMPLOYEE', 1, :e, NULL, '2026-09-30', 'y', 1, 'x', "
                     "'GENERATED', '{}'::jsonb, repeat('a', 64), repeat('b', 64), 1, 'x.pdf', 't', 'r', 'en', now(), "
                     "now())", c=a, le=own_entity, e=foreign_employee)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO document_access_log (company_id, document_id, action, at, details, created_at, "
                     "updated_at) VALUES (:c, :d, 'VIEW', now(), '{}'::jsonb, now(), now())",
                     c=a, d=seeded[b]["documents"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO document_notifications (company_id, document_id, channel, status, attached, "
                     "created_at, updated_at) VALUES (:c, :d, 'EMAIL', 'PENDING', false, now(), now())",
                     c=a, d=seeded[b]["documents"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO invoice_sources (company_id, invoice_id, source_type, source_id, active, created_at, "
                     "updated_at) VALUES (:c, :i, 'TASK', 2, true, now(), now())", c=a, i=seeded[b]["invoices"])
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO invoices (company_id, kind, mode, legal_entity_id, payee_employee_id, status, currency, "
                     "subtotal, tax_total, total, created_at, updated_at) VALUES (:c, 'INVOICE', 'INVOICE', :le, :e, "
                     "'DRAFT', 'INR', 0, 0, 0, now(), now())", c=a, le=own_entity, e=foreign_employee)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO self_billing_agreements (company_id, employee_id, legal_entity_id, reference, "
                     "effective_from, status, created_at, updated_at) VALUES (:c, :e, :le, 'X', '2026-01-01', 'ACTIVE', "
                     "now(), now())", c=a, e=own_employee, le=foreign_entity)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO document_requests (company_id, employee_id, doc_type, status, requested_by_user_id, "
                     "created_at, updated_at) VALUES (:c, :e, 'EMPLOYMENT_LETTER', 'PENDING', :u, now(), now())",
                     c=a, e=foreign_employee, u=own_user)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO payslip_pdf_secrets (company_id, employee_id, secret_enc, set_at, created_at, "
                     "updated_at) VALUES (:c, :e, 'x', now(), now(), now())", c=a, e=foreign_employee)
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO export_jobs (company_id, requested_by_user_id, kind, status, created_at, updated_at) "
                     "VALUES (:c, :u, 'REGISTER_CSV', 'QUEUED', now(), now())", c=a, u=tenants[b][3])
        run_employee_b = sql("SELECT run_employee_id, run_id FROM documents WHERE id = :d", d=seeded[b]["documents"])[0]
        expect_error(conn, FOREIGN_KEY,
                     "INSERT INTO document_generation_tasks (company_id, run_id, run_employee_id, doc_type, "
                     "document_version, status, created_at, updated_at) VALUES (:c, :r, :re, 'PAYSLIP_V2', 9, "
                     "'QUEUED', now(), now())", c=a, r=run_employee_b[1], re=run_employee_b[0])


# ---------------------------------------------------------------------------
# Object access (EC-DOC-01, EC-SEC-02, EC-SEC-05)
# ---------------------------------------------------------------------------
def test_ec_doc_01_another_employees_or_tenants_payslip_is_404_everywhere(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_doc = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    ivan, emma = w["logins"]["Ivan"], w["logins"]["Emma"]
    emma_link = emma.get(f"{ME}/payslips/{emma_doc}/download").json()["url"]
    for client in (ivan, w["beta"]["login"], w["beta"]["client"], w["cobalt"]["client"]):
        for url in (f"{PAY}/payslips/{emma_doc}", f"{ME}/payslips/{emma_doc}", f"{ME}/payslips/{emma_doc}/download",
                    f"{PAY}/documents/{emma_doc}", f"{ME}/documents/{emma_doc}",
                    f"{ME}/documents/{emma_doc}/download", emma_link):
            r = client.get(url)
            assert r.status_code == 404, (url, r.status_code, r.text)
            assert "68700" not in r.text and "Emma" not in r.text
        assert client.get(f"{PAY}/payslips/{emma_doc}/download").status_code in (403, 404)
    # The employee's own list never contains anyone else's payslip.
    assert [i["employee_id"] for i in ivan.get(f"{ME}/payslips").json()["items"]] == [w["emp"]["Ivan"]["id"]]
    # Ids that never existed look the same as foreign ones.
    assert ivan.get(f"{PAY}/payslips/999999").status_code == 404
    # A tenant's own HR list and counts never include other tenants' or out-of-scope documents.
    assert w["beta"]["client"].get(f"{PAY}/payslips").json()["total"] == 0
    assert w["clerk"].get(f"{PAY}/payslips").json()["total"] == 0  # scoped to AE01 (EC-SEC-05)
    assert w["clerk"].get(f"{PAY}/payslips/{emma_doc}").status_code == 404
    assert w["acme"].get(f"{PAY}/payslips").json()["total"] == 2
    assert ivan.get(f"{PAY}/payslips").status_code == 403  # SELF only: no HR list


def test_ec_sec_02_foreign_ids_are_404_on_every_state_changing_endpoint(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_doc = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    beta = w["beta"]["client"]  # Beta's founder: payroll manager of Beta only
    attacks = (
        ("post", f"{PAY}/payslips/{emma_doc}/publish", {"reason": "x"}),
        ("post", f"{PAY}/payslips/{emma_doc}/resend", None),
        ("post", f"{PAY}/payslips/publish", {"document_ids": [emma_doc], "reason": "x"}),
        ("post", f"{PAY}/payslips/zip", {"run_id": run_id}),
        ("post", f"{PAY}/runs/{run_id}/publish", {"reason": "x"}),
        ("post", f"{PAY}/runs/{run_id}/payslips/generate", None),
        ("post", f"{PAY}/runs/{run_id}/payslips/retry-failed", None),
        ("get", f"{PAY}/runs/{run_id}/payslips/generation", None),
        ("post", f"{PAY}/runs/{run_id}/documents/register", None),
        ("get", f"{PAY}/runs/{run_id}/documents/register/draft", None),
        ("post", f"{PAY}/runs/{run_id}/documents/funding-request", None),
        ("post", f"{PAY}/runs/{run_id}/documents/final-settlement", None),
        ("post", f"{PAY}/exports", {"kind": "REGISTER_CSV", "run_id": run_id}),
    )
    for method, url, body in attacks:
        r = getattr(beta, method)(url, json=body) if body is not None else getattr(beta, method)(url)
        assert r.status_code in (403, 404), (url, r.status_code, r.text)
        if r.status_code == 403:  # only when Beta's user lacks the permission altogether
            assert r.json()["code"] in ("FORBIDDEN",), url
    assert sql("SELECT count(*) FROM documents WHERE company_id = :c", c=w["beta"]["cid"])[0][0] == 0
    assert sql("SELECT status FROM documents WHERE id = :d", d=emma_doc)[0][0] in ("PUBLISHED", "EMAIL_SENT")


def test_register_and_funding_request_respect_scope_and_state(docs_world):
    w = docs_world
    run_id, _ = emma_run(w)
    generate(w["cid"], run_id)
    r = w["acme"].post(f"{PAY}/runs/{run_id}/documents/register")
    assert r.status_code == 201 and r.json()["doc_type"] == "PAYROLL_REGISTER" and r.json()["number"]
    again = w["acme"].post(f"{PAY}/runs/{run_id}/documents/register").json()
    assert again["id"] == r.json()["id"]  # one immutable register per finalized run
    assert w["acme"].get(f"{PAY}/runs/{run_id}/documents/register/draft").status_code == 409
    assert w["clerk"].post(f"{PAY}/runs/{run_id}/documents/register").status_code == 404
    assert w["logins"]["Emma"].post(f"{PAY}/runs/{run_id}/documents/register").status_code == 403
    funding = w["acme"].post(f"{PAY}/runs/{run_id}/documents/funding-request")
    assert funding.status_code == 201 and funding.json()["doc_type"] == "FUNDING_REQUEST"
    # A run that is calculated but not finalized only gets a watermarked draft register.
    draft_run, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 10, 1),
                                 [w["emp"]["Emma"]["id"]], status="APPROVED")
    sql("UPDATE payroll_runs SET status = 'REVIEW_REQUIRED' WHERE id = :r", r=draft_run)
    assert w["acme"].post(f"{PAY}/runs/{draft_run}/documents/register").status_code == 409
    assert w["acme"].post(f"{PAY}/runs/{draft_run}/documents/funding-request").status_code == 409
    r = w["acme"].get(f"{PAY}/runs/{draft_run}/documents/register/draft")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    from tests.payroll.test_payslips_v2 import pdf_text

    assert "DRAFT" in pdf_text(r.content)
    assert sql("SELECT count(*) FROM documents WHERE run_id = :r", r=draft_run)[0][0] == 0  # never stored
    # Registers are HR documents: the employee never sees them.
    register_id = again["id"]
    assert w["logins"]["Emma"].get(f"{PAY}/documents/{register_id}").status_code == 404
    assert w["logins"]["Emma"].get(f"{ME}/documents/{register_id}").status_code == 404


# ---------------------------------------------------------------------------
# Public verification (EC-DOC-18)
# ---------------------------------------------------------------------------
def test_ec_doc_18_verification_shows_issuer_and_masked_name_never_amounts(app, docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id, _, number, *_rest = payslip_of(rows[w["emp"]["Emma"]["id"]])
    public_id = sql("SELECT public_id FROM documents WHERE id = :d", d=doc_id)[0][0]
    anonymous = new_client(app)
    r = anonymous.get(f"/api/v1/documents/verify/{public_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"valid", "issuer", "document_type", "issue_date", "number", "name", "status"}
    assert body["valid"] is True and body["number"] == number and body["document_type"] == "Payslip"
    assert body["issuer"] and body["status"] == "ISSUED"
    assert body["name"] != w["emp"]["Emma"]["first_name"] + " " + w["emp"]["Emma"]["last_name"]
    assert "•" in body["name"] or "*" in body["name"]
    for amount in ("68700", "68,700", "70000", "50000", "1800"):
        assert amount not in r.text
    assert sql("SELECT count(*) FROM document_access_log WHERE document_id = :d AND action = 'VERIFY'",
               d=doc_id)[0][0] == 1
    assert anonymous.get("/api/v1/documents/verify/unknownunknownunknown00").status_code == 404
    assert anonymous.get("/api/v1/documents/verify/'%20OR%201=1").status_code == 404
    assert anonymous.get(f"/api/v1/documents/verify/{doc_id}").status_code == 404  # ids never verify
    codes = [anonymous.get(f"/api/v1/documents/verify/{public_id}").status_code for _ in range(40)]
    assert 429 in codes  # rate limited


def test_ec_doc_18_verification_is_bound_to_the_issuing_tenants_host(app, docs_world):
    """A document verifies on its own tenant's host (and the platform host), never on another tenant's or an
    unknown host: a white-label brand never vouches for another tenant's document."""
    from tests.white_label_helpers import PLATFORM_HOST, HostClient, tenant_host

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    url = f"/api/v1/documents/verify/{sql('SELECT public_id FROM documents WHERE id = :d', d=doc_id)[0][0]}"
    own = HostClient(app, tenant_host("acme-corp")).get(url)
    assert own.status_code == 200 and own.json()["issuer"], own.text
    assert HostClient(app, PLATFORM_HOST).get(url).status_code == 200
    for host in (tenant_host("beta-industries"), tenant_host("cobalt-labs"), "evil.example.com"):
        r = HostClient(app, host).get(url)
        assert r.status_code == 404, (host, r.status_code)
        assert "Acme" not in r.text and "Emma" not in r.text
    assert sql("SELECT count(*) FROM document_access_log WHERE document_id = :d AND action = 'VERIFY'",
               d=doc_id)[0][0] == 2  # failed lookups leave no trace on the document


def test_numbers_and_public_ids_are_not_guessable_and_differ_per_tenant(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    ids = [r[0] for r in sql("SELECT public_id FROM documents WHERE company_id = :c", c=w["cid"])]
    assert len(set(ids)) == len(ids) == 2 and all(len(i) >= 20 for i in ids)
    assert all(not i.isdigit() for i in ids)  # random, never derived from a row id


# ---------------------------------------------------------------------------
# Payment documents (EC-DOC-10) and employee-facing statements
# ---------------------------------------------------------------------------
def _batch(cid, entity, user_id):
    funding = sql(
        "INSERT INTO funding_accounts (company_id, legal_entity_id, name, holder_name, country, currency, "
        "account_number_enc, last4, routing, fingerprint, purpose, signatory_user_ids, formats, status, row_version, "
        "created_at, updated_at) VALUES (:c, :le, 'Main payroll', 'Acme', 'IN', 'INR', 'sealed', '9012', '{}'::jsonb, "
        "'fp-test', 'PAYROLL', ARRAY[:u], ARRAY['GENERIC_CSV'], 'ACTIVE', 1, now(), now()) RETURNING id",
        c=cid, le=entity, u=user_id)[0][0]
    return sql(
        "INSERT INTO payment_batches (company_id, reference, legal_entity_id, funding_account_id, method, currency, "
        "status, item_count, total_amount, totals, prepared_by_user_id, details, row_version, created_at, updated_at) "
        "VALUES (:c, 'BATCH-2026-09', :le, :f, 'MANUAL', 'INR', 'DRAFT', 0, 0, '{}'::jsonb, :u, '{}'::jsonb, 1, now(), "
        "now()) RETURNING id", c=cid, le=entity, f=funding, u=user_id)[0][0]


def test_ec_doc_10_payment_advice_only_with_the_matching_payment_state(app, docs_world):
    w = docs_world
    fin = login_as(app, w["acme"], create_employee(w["acme"], "Fin", hire_date="2024-01-15"), ["finance_payments"])
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    failed = add_instruction(w["cid"], run_id, rows[emma_id], emma_id, w["le01"], w["group"], status="FAILED",
                             channel="API")
    submitted = add_instruction(w["cid"], run_id, rows[ivan_id], ivan_id, w["le01"], w["group"], status="SUBMITTED")
    advice = PAY + "/payment-instructions/{}/documents/payment-advice"
    r = fin.post(advice.format(failed), json={"kind": "INSTRUCTION"})
    assert r.status_code == 409 and r.json()["code"] == "PAYMENT_NOT_SUBMITTED"
    r = fin.post(advice.format(submitted), json={"kind": "CONFIRMATION"})
    assert r.status_code == 409 and r.json()["code"] == "PAYMENT_NOT_SETTLED"  # submitted is not settled
    assert sql("SELECT count(*) FROM documents WHERE doc_type = 'PAYMENT_ADVICE'")[0][0] == 0
    r = fin.post(advice.format(submitted), json={"kind": "INSTRUCTION"})
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["doc_type"] == "PAYMENT_ADVICE" and doc["status"] == "PUBLISHED" and doc["employee_id"] == ivan_id
    ivan = w["logins"]["Ivan"]
    text_ = pdf_text(ivan.get(ivan.get(f"{ME}/documents/{doc['id']}/download").json()["url"]).content)
    assert "Payment instruction" in text_ and "••••4321" in text_
    assert w["logins"]["Emma"].get(f"{ME}/documents/{doc['id']}").status_code == 404
    settled_row = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 10, 1), [emma_id])[1][emma_id]
    settled = add_instruction(w["cid"], run_id, settled_row, emma_id, w["le01"], w["group"], status="SETTLED",
                              channel="API", evidence="VERIFIED")
    r = fin.post(advice.format(settled), json={"kind": "CONFIRMATION"})
    assert r.status_code == 201 and r.json()["title"] == "Payment confirmation"
    assert fin.post(advice.format(settled), json={"kind": "CONFIRMATION"}).json()["id"] == r.json()["id"]
    assert fin.post(advice.format(settled), json={"kind": "RECEIPT"}).status_code == 422
    # Without a payments permission, or for another tenant: denied.
    assert w["acme"].post(advice.format(settled), json={"kind": "CONFIRMATION"}).status_code == 403
    beta_fin = login_as(app, w["beta"]["client"], create_employee(w["beta"]["client"], "Bea", hire_date="2024-01-15"),
                        ["finance_payments"])
    assert beta_fin.post(advice.format(settled), json={"kind": "CONFIRMATION"}).status_code == 404


def test_bank_transfer_report_is_masked_and_versioned_by_content(app, docs_world):
    w = docs_world
    fin = login_as(app, w["acme"], create_employee(w["acme"], "Fin", hire_date="2024-01-15"), ["finance_payments"])
    run_id, rows = emma_run(w)
    batch = _batch(w["cid"], w["le01"], fin.user_id)
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    instruction = add_instruction(w["cid"], run_id, rows[emma_id], emma_id, w["le01"], w["group"], status="SUBMITTED")
    sql("UPDATE payment_instructions SET batch_id = :b WHERE id = :i", b=batch, i=instruction)
    url = f"{PAY}/payment-batches/{batch}/documents/bank-transfer-report"
    first = fin.post(url)
    assert first.status_code == 201, first.text
    assert fin.post(url).json()["id"] == first.json()["id"]  # unchanged batch: same document
    text_ = pdf_text(fin.get(fin.get(f"{PAY}/documents/{first.json()['id']}/download").json()["url"]).content)
    assert "••••4321" in text_ and "68700.00" in text_
    assert w["logins"]["Emma"].get(f"{ME}/documents/{first.json()['id']}").status_code == 404  # finance only
    second = add_instruction(w["cid"], run_id, rows[ivan_id], ivan_id, w["le01"], w["group"], status="SUBMITTED")
    sql("UPDATE payment_instructions SET batch_id = :b WHERE id = :i", b=batch, i=second)
    newer = fin.post(url).json()
    assert newer["id"] != first.json()["id"] and newer["version"] == 2
    assert w["acme"].post(url).status_code == 403
    assert w["beta"]["client"].post(url).status_code in (403, 404)


def test_salary_certificate_request_needs_another_persons_approval(docs_world):
    w = docs_world
    run_id, _ = emma_run(w)
    generate(w["cid"], run_id)
    emma = w["logins"]["Emma"]
    r = emma.post(f"{ME}/document-requests", json={"doc_type": "SALARY_CERTIFICATE", "include_amounts": True,
                                                    "purpose": "Visa application"})
    assert r.status_code == 201 and r.json()["status"] == "PENDING"
    request_id = r.json()["id"]
    assert emma.post(f"{ME}/document-requests", json={"doc_type": "SALARY_CERTIFICATE"}).status_code == 409
    assert emma.post(f"{ME}/document-requests", json={"doc_type": "SALARY_CERTIFICATE", "status": "ISSUED"}
                     ).status_code == 422
    assert emma.post(f"{PAY}/document-requests/{request_id}/approve", json={}).status_code == 403
    assert w["logins"]["Ivan"].get(f"{PAY}/document-requests").status_code == 403
    assert w["clerk"].post(f"{PAY}/document-requests/{request_id}/approve", json={}).status_code == 404
    assert w["beta"]["client"].post(f"{PAY}/document-requests/{request_id}/approve", json={}).status_code == 404
    assert w["acme"].post(f"{PAY}/document-requests/{request_id}/reject", json={"note": " "}).status_code == 422
    listed = w["acme"].get(f"{PAY}/document-requests").json()
    assert [i["id"] for i in listed["items"]] == [request_id]
    r = w["acme"].post(f"{PAY}/document-requests/{request_id}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "ISSUED", r.text
    document_id = r.json()["document_id"]
    mine = emma.get(f"{ME}/documents").json()["items"]
    assert any(d["id"] == document_id and d["doc_type"] == "SALARY_CERTIFICATE" for d in mine)
    text_ = pdf_text(emma.get(emma.get(f"{ME}/documents/{document_id}/download").json()["url"]).content)
    assert "This is to certify" in text_ and "70000.00" in text_ and "Visa application" in text_
    assert w["acme"].post(f"{PAY}/document-requests/{request_id}/approve", json={}).status_code == 409


def test_statements_for_self_and_staff_only(docs_world):
    w = docs_world
    emma, ivan = w["logins"]["Emma"], w["logins"]["Ivan"]
    r = emma.post(f"{ME}/reimbursement-statement", json={"start": "2026-04-01", "end": "2026-09-30"})
    assert r.status_code == 201 and r.json()["doc_type"] == "REIMBURSEMENT_STATEMENT" and r.json()["number"] is None
    again = emma.post(f"{ME}/reimbursement-statement", json={"start": "2026-04-01", "end": "2026-09-30"}).json()
    assert again["version"] == 2  # a statement is a snapshot per request
    emma_id = w["emp"]["Emma"]["id"]
    url = f"{PAY}/employees/{emma_id}/documents/reimbursement-statement"
    assert ivan.post(url, json={"start": "2026-04-01", "end": "2026-09-30"}).status_code == 403
    assert w["clerk"].post(url, json={"start": "2026-04-01", "end": "2026-09-30"}).status_code == 403
    assert w["acme"].post(url, json={"start": "2026-09-30", "end": "2026-04-01"}).status_code == 400
    assert w["acme"].post(url, json={"start": "2026-04-01", "end": "2026-09-30"}).status_code == 201
    assert w["beta"]["client"].post(url, json={"start": "2026-04-01", "end": "2026-09-30"}).status_code in (403, 404)
    assert w["acme"].post(f"{PAY}/loans/999999/documents/statement").status_code == 404
    assert w["acme"].post(f"{PAY}/employees/{emma_id}/documents/increment-letter",
                          json={"compensation_assignment_id": 999999}).status_code == 404


def test_final_settlement_statements_only_from_finalized_settlement_runs(docs_world):
    w = docs_world
    run_id, _ = emma_run(w)
    generate(w["cid"], run_id)
    assert w["acme"].post(f"{PAY}/runs/{run_id}/documents/final-settlement").status_code == 409
    settlement, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 10, 1), [w["emp"]["Emma"]["id"]],
                                  run_type="FINAL_SETTLEMENT")
    r = w["acme"].post(f"{PAY}/runs/{settlement}/documents/final-settlement")
    assert r.status_code == 201, r.text
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["doc_type"] == "FINAL_SETTLEMENT_STATEMENT" and items[0]["status"] == "PUBLISHED"
    assert w["logins"]["Emma"].get(f"{ME}/documents/{items[0]['id']}").status_code == 200
    assert w["logins"]["Ivan"].get(f"{ME}/documents/{items[0]['id']}").status_code == 404
    assert w["clerk"].post(f"{PAY}/runs/{settlement}/documents/final-settlement").status_code == 404

