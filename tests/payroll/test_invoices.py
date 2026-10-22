"""Contractor invoices, self-billed invoices and credit notes (3.6 §10; INVOICE machine).

EC-DOC-07 (employees never get invoices; contractors only for approved, genuine transactions), EC-DOC-08
(gapless numbers allocated in the issuing transaction), EC-DOC-09 (cancelled by a credit note; the original
stays; its number is never reused).
"""

from datetime import date
from decimal import Decimal

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import login_as
from tests.payroll.test_payslips_v2 import ME, PAY, docs_world, pdf_text  # noqa: F401  (docs_world is a fixture)
from tests.payroll.test_wage_inputs import audit_actions, place, role, session, sql, user_with_role

INVOICES = f"{PAY}/invoices"


def make_contractor(admin, cid, entity, group, name="Carl"):
    employee = create_employee(admin, name, hire_date="2024-01-15")
    place(employee["id"], entity, group)
    found = sql("SELECT id FROM employment_types WHERE company_id = :c AND code = 'CONTRACTOR'", c=cid)
    type_id = found[0][0] if found else sql(
        "INSERT INTO employment_types (company_id, code, name, worker_class, status, created_at, updated_at) "
        "VALUES (:c, 'CONTRACTOR', 'Contractor', 'CONTRACTOR', 'ACTIVE', now(), now()) RETURNING id", c=cid)[0][0]
    sql("UPDATE employee_assignments SET employment_type_id = :t WHERE employee_id = :e", t=type_id, e=employee["id"])
    return employee


def make_task(cid, assignees, *, quantity="10", accepted=None, rate="150.00", currency="INR", status="APPROVED",
              title="Site survey"):
    task_id = sql(
        "INSERT INTO tasks (company_id, title, task_type, quantity, unit, rate, rate_currency, status, "
        "accepted_quantity, approved_on, row_version, created_at, updated_at) VALUES (:c, :t, 'SURVEY', :q, 'unit', "
        ":r, :cur, :s, :a, :ao, 1, now(), now()) RETURNING id",
        c=cid, t=title, q=Decimal(quantity), r=Decimal(rate), cur=currency, s=status,
        a=Decimal(accepted) if accepted else None, ao=date(2026, 9, 10) if status == "APPROVED" else None)[0][0]
    for employee_id, split in assignees:
        sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) "
            "VALUES (:c, :t, :e, :s, now(), now())", c=cid, t=task_id, e=employee_id, s=Decimal(split))
    return task_id


@pytest.fixture
def inv_world(app, docs_world):
    w = docs_world
    acme, cid = w["acme"], w["cid"]
    carl = make_contractor(acme, cid, w["le01"], w["group"])
    w["carl"] = carl
    w["carl_login"] = login_as(app, acme, carl, ["employee"])
    w["fin"] = login_as(app, acme, create_employee(acme, "Fin", hire_date="2024-01-15"), ["finance_payments"])
    w["fay"] = login_as(app, acme, create_employee(acme, "Fay", hire_date="2024-01-15"), ["finance_payments"])
    w["t1"] = make_task(cid, [(carl["id"], "100")], quantity="10", rate="150.00")
    w["t2"] = make_task(cid, [(carl["id"], "50"), (w["emp"]["Emma"]["id"], "50")], quantity="4", accepted="3",
                        rate="200.00", title="Report writing")
    return w


def draft(client, w, task_ids, mode="INVOICE", expect=201, **extra):
    body = {"mode": mode, "payee_employee_id": w["carl"]["id"], "task_ids": task_ids, **extra}
    if mode == "INVOICE":
        body.setdefault("external_number", "CARL-77")
    r = client.post(INVOICES, json=body)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Who and what (EC-DOC-07)
# ---------------------------------------------------------------------------
def test_ec_doc_07_employees_never_get_invoices_and_only_approved_tasks_bill(inv_world):
    w = inv_world
    fin = w["fin"]
    r = fin.post(INVOICES, json={"mode": "INVOICE", "payee_employee_id": w["emp"]["Emma"]["id"],
                                 "task_ids": [w["t2"]], "external_number": "E-1"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_A_CONTRACTOR"
    submitted = make_task(w["cid"], [(w["carl"]["id"], "100")], status="SUBMITTED")
    assert draft(fin, w, [submitted], expect=409)["code"] == "TASK_NOT_BILLABLE"
    emma_only = make_task(w["cid"], [(w["emp"]["Emma"]["id"], "100")])
    assert draft(fin, w, [emma_only], expect=404)["code"] == "NOT_FOUND"  # not the payee's task
    foreign = make_task(w["beta"]["cid"], [(w["beta"]["employee"]["id"], "100")])
    assert draft(fin, w, [foreign], expect=404)["code"] == "NOT_FOUND"  # another tenant's task
    no_rate = sql("INSERT INTO tasks (company_id, title, task_type, quantity, unit, status, approved_on, row_version, "
                  "created_at, updated_at) VALUES (:c, 'x', 'SURVEY', 1, 'unit', 'APPROVED', '2026-09-01', 1, now(), "
                  "now()) RETURNING id", c=w["cid"])[0][0]
    sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) VALUES "
        "(:c, :t, :e, 100, now(), now())", c=w["cid"], t=no_rate, e=w["carl"]["id"])
    assert draft(fin, w, [no_rate], expect=409)["code"] == "TASK_RATE_MISSING"
    assert draft(fin, w, [w["t1"]], external_number=None, expect=400)["code"] == "EXTERNAL_NUMBER_REQUIRED"
    # Server-owned fields are refused (strict bodies).
    for field, value in (("status", "PAID"), ("total", "1.00"), ("number", "INV-1"), ("company_id", 2)):
        r = fin.post(INVOICES, json={"mode": "INVOICE", "payee_employee_id": w["carl"]["id"], "task_ids": [w["t1"]],
                                     "external_number": "X", field: value})
        assert r.status_code == 422, field
    # Without a payments permission: 403; a payslip-only role cannot create invoices.
    assert w["logins"]["Emma"].post(INVOICES, json={"mode": "INVOICE", "payee_employee_id": w["carl"]["id"],
                                                    "task_ids": [w["t1"]], "external_number": "X"}).status_code == 403
    assert w["acme"].get(INVOICES).status_code == 403  # payroll_manager does not hold payment permissions
    assert sql("SELECT count(*) FROM invoices")[0][0] == 0


# ---------------------------------------------------------------------------
# Lifecycle and numbering (EC-DOC-08, EC-DOC-09)
# ---------------------------------------------------------------------------
def test_ec_doc_09_invoice_lifecycle_credit_note_and_numbers_never_reused(inv_world):
    from app.payroll.services import invoices

    w = inv_world
    fin, fay, carl = w["fin"], w["fay"], w["carl_login"]
    invoice = draft(fin, w, [w["t1"], w["t2"]], taxes=[{"label": "GST", "rate_percent": "18"}],
                    seller={"address": "12 Market Rd", "tax_registration": "29ABCDE1234F1Z5"})
    assert invoice["status"] == "DRAFT" and invoice["number"] is None
    assert [ln["amount"] for ln in invoice["lines"]] == ["1500.00", "300.00"]  # 3 accepted × 50% × 200.00
    assert (invoice["subtotal"]["amount"], invoice["tax_total"]["amount"], invoice["total"]["amount"]) == (
        "1800.00", "324.00", "2124.00")
    assert draft(fin, w, [w["t1"]], expect=409)["code"] == "TASK_ALREADY_INVOICED"
    assert carl.get(f"{ME}/invoices").json()["items"] == []  # drafts are never shown to the payee
    r = fin.post(f"{INVOICES}/{invoice['id']}/issue", json={"row_version": 99})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    issued = fin.post(f"{INVOICES}/{invoice['id']}/issue", json={}).json()
    assert issued["status"] == "ISSUED" and issued["number"].startswith("INV/LE01/") and issued["number"].endswith(
        "000001")
    doc = sql("SELECT doc_type, number, status FROM documents WHERE id = :d", d=issued["document_id"])[0]
    assert doc == ("INVOICE", issued["number"], "PUBLISHED")
    assert fin.post(f"{INVOICES}/{invoice['id']}/issue", json={}).status_code == 409
    # The contractor sees the issued invoice and its PDF (their own only).
    assert [i["id"] for i in carl.get(f"{ME}/invoices").json()["items"]] == [invoice["id"]]
    url = carl.get(f"{ME}/documents/{issued['document_id']}/download").json()["url"]
    text_ = pdf_text(carl.get(url).content)
    assert "Invoice" in text_ and "2,124.00" in text_ and "CARL-77" in text_ and issued["number"] in text_
    assert w["logins"]["Emma"].get(f"{ME}/invoices/{invoice['id']}").status_code == 404
    # Maker-checker: the issuer never approves.
    r = fin.post(f"{INVOICES}/{invoice['id']}/approve", json={})
    assert r.status_code == 403
    approved = fay.post(f"{INVOICES}/{invoice['id']}/approve", json={}).json()
    assert approved["status"] == "APPROVED" and approved["approved_by_user_id"] != approved["issued_by_user_id"]
    # Never "paid" by a click: only payment evidence (system hook) pays an invoice.
    assert fay.post(f"{INVOICES}/{invoice['id']}/pay", json={}).status_code in (404, 405)
    with session(w["cid"]) as (db, _):
        with pytest.raises(Exception):
            invoices.record_payment(db, w["cid"], invoice["id"], "")
        db.rollback()
    # Cancellation: a credit note; the original stays with its number.
    assert fay.post(f"{INVOICES}/{invoice['id']}/credit", json={}).status_code == 422
    credited = fay.post(f"{INVOICES}/{invoice['id']}/credit", json={"reason": "Duplicate billing"}).json()
    assert credited["status"] == "CREDITED" and credited["number"] == issued["number"]
    note = sql("SELECT id, kind, number, status, total, credit_of_invoice_id, document_id FROM invoices "
               "WHERE kind = 'CREDIT_NOTE'")[0]
    assert note[1:6] == ("CREDIT_NOTE", note[2], "ISSUED", Decimal("2124.0000"), invoice["id"])
    assert note[2].startswith("CN/LE01/") and note[2].endswith("000001") and note[6] is not None
    assert sql("SELECT doc_type FROM documents WHERE id = :d", d=note[6])[0][0] == "CREDIT_NOTE"
    # The credited tasks may be billed again; the new invoice gets the next number, never the old one.
    again = draft(fin, w, [w["t1"]])
    reissued = fin.post(f"{INVOICES}/{again['id']}/issue", json={}).json()
    assert reissued["number"].endswith("000002") and reissued["number"] != issued["number"]
    assert "INVOICE_ISSUE" in audit_actions(w["cid"], "invoice", invoice["id"])
    assert "INVOICE_CREDIT" in audit_actions(w["cid"], "invoice", invoice["id"])


def test_record_payment_needs_settlement_evidence(inv_world):
    from app.core.errors import AppError
    from app.payroll.services import invoices

    w = inv_world
    invoice = draft(w["fin"], w, [w["t1"]])
    w["fin"].post(f"{INVOICES}/{invoice['id']}/issue", json={})
    with session(w["cid"]) as (db, _):
        with pytest.raises(AppError) as info:  # ISSUED -> PAID is not a transition (needs approval first)
            invoices.record_payment(db, w["cid"], invoice["id"], "STMT-1")
        assert info.value.status_code == 409
        db.rollback()
    w["fay"].post(f"{INVOICES}/{invoice['id']}/approve", json={})
    with session(w["cid"]) as (db, _):
        paid = invoices.record_payment(db, w["cid"], invoice["id"], "STMT-2026-09-30-001")
        db.commit()
        assert paid.status == "PAID"


def test_self_billed_invoice_requires_an_active_agreement(inv_world):
    w = inv_world
    fin = w["fin"]
    invoice = draft(fin, w, [w["t1"]], mode="SELF_BILLED")
    r = fin.post(f"{INVOICES}/{invoice['id']}/issue", json={})
    assert r.status_code == 409 and r.json()["code"] == "NO_SELF_BILLING_AGREEMENT"
    r = fin.post(f"{PAY}/self-billing-agreements", json={"employee_id": w["emp"]["Emma"]["id"],
                                                         "legal_entity_id": w["le01"], "reference": "SBA-0",
                                                         "effective_from": "2024-01-01"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_A_CONTRACTOR"
    r = fin.post(f"{PAY}/self-billing-agreements", json={"employee_id": w["carl"]["id"], "legal_entity_id": w["le01"],
                                                         "reference": "SBA-2026-01", "effective_from": "2024-01-01"})
    assert r.status_code == 201, r.text
    agreement = r.json()
    issued = fin.post(f"{INVOICES}/{invoice['id']}/issue", json={}).json()
    assert issued["status"] == "ISSUED" and issued["number"].startswith("SB-INV/LE01/")
    text_ = pdf_text(w["fin"].get(w["fin"].get(f"{INVOICES}/{invoice['id']}/download").json()["url"]).content)
    assert "Self-billed invoice" in text_ and "SBA-2026-01" in text_
    assert [a["id"] for a in fin.get(f"{PAY}/self-billing-agreements").json()["items"]] == [agreement["id"]]
    r = fin.post(f"{PAY}/self-billing-agreements/{agreement['id']}/revoke", json={"reason": "Contract ended"})
    assert r.status_code == 200 and r.json()["status"] == "REVOKED"
    second = draft(fin, w, [w["t2"]], mode="SELF_BILLED")
    assert fin.post(f"{INVOICES}/{second['id']}/issue", json={}).json()["code"] == "NO_SELF_BILLING_AGREEMENT"


def test_invoices_are_scoped_by_legal_entity_and_tenant(app, inv_world):
    w = inv_world
    invoice = draft(w["fin"], w, [w["t1"]])
    ae_role = role(w["acme"], "AE payments", "LEGAL_ENTITY", ("payroll:payment:prepare", "payroll:payment:approve"))
    gulf = user_with_role(app, w["acme"], create_employee(w["acme"], "Gulf", hire_date="2024-01-15"), ae_role,
                          [w["ae"]])
    assert gulf.get(f"{INVOICES}/{invoice['id']}").status_code == 404
    assert gulf.get(INVOICES).json()["total"] == 0
    assert gulf.post(f"{INVOICES}/{invoice['id']}/issue", json={}).status_code == 404
    assert gulf.post(INVOICES, json={"mode": "INVOICE", "payee_employee_id": w["carl"]["id"], "task_ids": [w["t2"]],
                                     "external_number": "X"}).status_code == 404
    beta = w["beta"]
    beta_fin = login_as(app, beta["client"], create_employee(beta["client"], "Bea", hire_date="2024-01-15"),
                        ["finance_payments"])
    assert beta_fin.get(f"{INVOICES}/{invoice['id']}").status_code == 404
    assert beta_fin.get(INVOICES).json()["total"] == 0
    assert beta_fin.post(f"{INVOICES}/{invoice['id']}/credit", json={"reason": "x"}).status_code == 404
    assert beta_fin.post(INVOICES, json={"mode": "INVOICE", "payee_employee_id": w["carl"]["id"],
                                         "task_ids": [w["t1"]], "external_number": "X"}).status_code == 404
    assert beta_fin.post(f"{PAY}/self-billing-agreements", json={
        "employee_id": w["carl"]["id"], "legal_entity_id": w["le01"], "reference": "X",
        "effective_from": "2024-01-01"}).status_code == 404
