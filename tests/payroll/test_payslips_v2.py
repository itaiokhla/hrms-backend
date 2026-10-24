"""Payslips v2: the view model, rendering, templates, languages, labels, payment status from evidence and the
v2 document namespace (3.6 §3-§4, §6; 01 §5.7; decisions I-07, I-15, I-19).

EC-DOC-03 (re-render from the frozen content gives the same bytes; issued documents never change),
EC-DOC-13 (bilingual right-to-left payslip), EC-DOC-23 (embedded Noto fonts: ₹, د.إ and Devanagari).

The fixtures and helpers here (``docs_world``, ``finalized_run``, ``generate``, ``pdf_text`` ...) are shared with
test_documents_numbering.py, test_publication.py, test_downloads.py, test_invoices.py and
test_document_isolation.py. Runs are written directly (as finalized results) so these tests depend only on the
run tables, not on the calculation pipeline.
"""

import hashlib
import itertools
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_compensation import login_as
from tests.payroll.test_wage_inputs import CHECK, expect_error, pay_group, place, session, set_setting, sql, tenant

PAY = "/api/v1/payroll"
ME = "/api/v1/me/payroll"
_numbers = itertools.count(1)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def entity_of(cid, code="LE01"):
    return sql("SELECT id FROM legal_entities WHERE company_id = :c AND code = :code", c=cid, code=code)[0][0]


def user_id_of(email):
    return sql("SELECT id FROM users WHERE email = :m", m=email)[0][0]


def calendar(cid, group):
    found = sql("SELECT id FROM pay_calendars WHERE company_id = :c AND pay_group_id = :g", c=cid, g=group)
    if found:
        return found[0][0]
    return sql("INSERT INTO pay_calendars (company_id, pay_group_id, frequency, rules, effective_from, status, "
               "row_version, created_at, updated_at) VALUES (:c, :g, 'MONTHLY', '{\"monthly_start_day\": 1}'::jsonb, "
               "'2020-01-01', 'ACTIVE', 1, now(), now()) RETURNING id", c=cid, g=group)[0][0]


def period(cid, group, start, *, pay_date=None, tax_year="2026-27"):
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    found = sql("SELECT id FROM pay_periods WHERE company_id = :c AND pay_group_id = :g AND period_start = :s",
                c=cid, g=group, s=start)
    if found:
        return found[0][0]
    pay = pay_date or end
    return sql(
        "INSERT INTO pay_periods (company_id, pay_calendar_id, pay_group_id, frequency, period_start, period_end, "
        "attendance_start, attendance_end, pay_date, nominal_pay_date, input_cutoff_on, approval_deadline_on, "
        "payment_submit_by, tax_year, number_in_tax_year, is_last_in_tax_year, is_transition, status, "
        "pay_date_warnings, row_version, created_at, updated_at) VALUES (:c, :cal, :g, 'MONTHLY', :s, :e, :s, :e, "
        ":p, :p, :p, :p, :p, :ty, 1, false, false, 'CLOSED', '[]'::jsonb, 1, now(), now()) RETURNING id",
        c=cid, cal=calendar(cid, group), g=group, s=start, e=end, p=pay, ty=tax_year)[0][0]


def assignment_of(employee_id):
    return sql("SELECT id FROM employee_assignments WHERE employee_id = :e AND is_primary ORDER BY id DESC LIMIT 1",
               e=employee_id)[0][0]


DEFAULT_LINES = (
    ("BASIC", "EARNING", "50000.00", {}),
    ("HRA", "EARNING", "20000.00", {}),
    ("PF_EMPLOYEE", "DEDUCTION", "1800.00", {"statutory": True}),
    ("TRAVEL", "REIMBURSEMENT", "500.00", {}),
    ("EMPLOYER_PF", "EMPLOYER_CONTRIBUTION", "1800.00", {"statutory": True}),
    ("RETENTION_ACCRUAL", "EARNING", "999.00", {"visibility": "HR_ONLY"}),
)


def finalized_run(cid, entity, group, start, employees, *, currency="INR", run_type="REGULAR", reverses=None,
                  lines=DEFAULT_LINES, status="FINALIZED", label=None):
    """A run of ``employees`` (ids) with the given lines per employee, written as finalized results.

    Returns (run_id, {employee_id: run_employee_id}).
    """
    period_id = period(cid, group, start)
    number = f"PR-{start.year}-{next(_numbers) + 100000:06d}"
    run_id = sql(
        "INSERT INTO payroll_runs (company_id, number, label, legal_entity_id, pay_group_id, pay_period_id, run_type, "
        "status, regular_sequence, currency, reverses_run_id, selection, pack_version_ids, totals, population, stale, "
        "stale_reasons, environment, row_version, status_reason, created_at, updated_at) VALUES (:c, :n, :l, :le, :g, "
        ":p, :t, 'APPROVED', :seq, :cur, :rev, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, false, "
        "'[]'::jsonb, 'test', 1, :reason, now(), now()) RETURNING id",
        c=cid, n=number, l=label or f"{start:%B %Y} run", le=entity, g=group, p=period_id, t=run_type,
        seq=1 if run_type == "REGULAR" else None, cur=currency, rev=reverses,
        reason="Wrong bank file" if run_type == "REVERSAL" else None)[0][0]
    rows = {}
    for employee_id in employees:
        earnings = sum(Decimal(a) for _, kind, a, o in lines if kind == "EARNING" and o.get("visibility") != "HR_ONLY")
        deductions = sum(Decimal(a) for _, kind, a, _o in lines if kind == "DEDUCTION")
        reimbursements = sum(Decimal(a) for _, kind, a, _o in lines if kind == "REIMBURSEMENT")
        employer = sum(Decimal(a) for _, kind, a, _o in lines if kind == "EMPLOYER_CONTRIBUTION")
        net = earnings - deductions + reimbursements
        rows[employee_id] = sql(
            "INSERT INTO payroll_run_employees (company_id, run_id, employee_id, employee_assignment_id, status, gross, "
            "deductions, reimbursements, net, employer_cost, currency, flags, row_version, created_at, updated_at) "
            "VALUES (:c, :r, :e, :a, 'CALCULATED', :g, :d, :re, :n, :ec, :cur, '[]'::jsonb, 1, now(), now()) "
            "RETURNING id", c=cid, r=run_id, e=employee_id, a=assignment_of(employee_id), g=earnings, d=deductions,
            re=reimbursements, n=net, ec=earnings + employer, cur=currency)[0][0]
        for order, (code, kind, amount, options) in enumerate(lines):
            sql("INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, amount, "
                "currency, tags, visibility, sources, trace, sort_order, statutory, fixture, period_ref, explanation, "
                "created_at, updated_at) VALUES (:c, :r, :re, :code, :k, 4, :a, :cur, '[]'::jsonb, :v, '[]'::jsonb, "
                "'{}'::jsonb, :o, :st, :fx, CAST(:pr AS jsonb), :ex, now(), now())",
                c=cid, r=run_id, re=rows[employee_id], code=code, k=kind, a=Decimal(amount), cur=currency,
                v=options.get("visibility", "EMPLOYEE_VISIBLE"), o=order, st=bool(options.get("statutory")),
                fx=bool(options.get("fixture")), pr=options.get("period_ref"), ex=options.get("explanation"))
    if status != "APPROVED":
        sql("UPDATE payroll_runs SET status = :s, finalized_at = now() WHERE id = :r", s=status, r=run_id)
    return run_id, rows


def generate(cid, run_id):
    """The outbox handler for ``payroll.run.finalized``, committed."""
    from app.payroll.services import publication

    with session(cid) as (db, _):
        result = publication.handle_run_finalized(db, cid, run_id)
        db.commit()
    return result


def payslip_of(run_employee_id, version=None):
    rows = sql("SELECT id, status, number, version, sha256, storage_path, public_id FROM documents "
               "WHERE doc_type = 'PAYSLIP_V2' AND run_employee_id = :r ORDER BY version", r=run_employee_id)
    if version is None:
        return rows[-1] if rows else None
    return next((r for r in rows if r[3] == version), None)


def content_of(document_id):
    return sql("SELECT content FROM documents WHERE id = :d", d=document_id)[0][0]


def document_pdf(cid, document_id):
    from app.models.pay_documents import PayDocument
    from app.payroll.services import documents

    with session(cid) as (db, _):
        doc = db.get(PayDocument, document_id)
        data, rerendered = documents.document_bytes(db, None, doc)
        db.commit()
    return data, rerendered


def pdf_text(data):
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


def pdf_fonts(data):
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return {font[3] for page in doc for font in page.get_fonts()}


def minimal_tenant(app, name):
    client = signup_company(app, name)
    cid = client.company_id
    entity = entity_of(cid)
    group = pay_group(cid, entity, "PG-X")
    employee = create_employee(client, f"{name.split()[0]}Emp", hire_date="2024-01-15")
    place(employee["id"], entity, group)
    login = login_as(app, client, employee, ["employee"])
    return {"client": client, "cid": cid, "entity": entity, "group": group, "employee": employee, "login": login}


@pytest.fixture
def docs_world(app):
    """Acme (LE01, INR pay group PG-IN): Emma and Ivan with self-service logins, the founder (payroll manager),
    a pay-group clerk scoped to another entity (AE01) and two finance users. Beta and Cobalt: minimal tenants."""
    from tests.payroll.test_wage_inputs import role, user_with_role

    acme = signup_company(app, "Acme Corp")
    cid = acme.company_id
    le01 = entity_of(cid)
    group = pay_group(cid, le01, "PG-IN")
    emp = {name: create_employee(acme, name, hire_date="2024-01-15") for name in ("Emma", "Ivan")}
    for e in emp.values():
        place(e["id"], le01, group)
    logins = {name: login_as(app, acme, e, ["employee"]) for name, e in emp.items()}
    r = acme.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Acme Gulf FZ-LLC",
                                                   "country_code": "AE", "base_currency": "AED",
                                                   "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    ae = r.json()["id"]
    clerk_role = role(acme, "AE payslip clerk", "LEGAL_ENTITY", ("payroll:payslip:read", "payroll:payslip:publish",
                                                                "payroll:reports:read", "payroll:reports:export"))
    clerk = user_with_role(app, acme, create_employee(acme, "Cleo", hire_date="2024-01-15"), clerk_role, [ae])
    # The starter profile e-mails every published payslip, which moves it on to EMAIL_SENT at once (jobs run
    # inline in tests). Publication tests assert the exact PUBLISHED state, so e-mail is switched off here and
    # switched on explicitly by the e-mail tests (test_publication.py).
    set_setting(cid, "payslip.email_notify", False)
    return {"acme": acme, "cid": cid, "le01": le01, "group": group, "emp": emp, "logins": logins, "ae": ae,
            "clerk": clerk, "beta": minimal_tenant(app, "Beta Industries"),
            "cobalt": minimal_tenant(app, "Cobalt Labs")}


def emma_run(w, start=date(2026, 9, 1), **kwargs):
    return finalized_run(w["cid"], w["le01"], w["group"], start, [w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]],
                         **kwargs)


# ---------------------------------------------------------------------------
# View model and content (3.6 §3)
# ---------------------------------------------------------------------------
def test_payslip_view_model_sections_hr_only_excluded_and_labels(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id, status, number, version, sha, path, public_id = payslip_of(rows[w["emp"]["Emma"]["id"]])
    assert status == "PUBLISHED" and version == 1 and path
    assert re.match(r"^PS/LE01/\d{4}(-\d{2})?/000001$", number), number
    content = content_of(doc_id)
    assert [i["code"] for i in content["earnings"]] == ["BASIC", "HRA"]  # HR_ONLY never on the payslip
    assert [i["code"] for i in content["deductions"]] == ["PF_EMPLOYEE"]
    assert [i["code"] for i in content["reimbursements"]] == ["TRAVEL"]
    assert content["employer_contributions"] == []  # payslip.show_employer_contributions is OFF by default
    assert content["totals"]["net"] == "68700.00" and content["totals"]["gross"] == "70000.00"
    assert content["totals"]["net_words"].startswith("Sixty-eight thousand seven hundred")
    assert content["payment"]["key"] == "SCHEDULED"  # no payment evidence yet: never "Paid"
    # PF lines without a verified (or fixture) pack are never presented as compliant (I-07, I-15).
    assert content["labels"] == ["Statutory deductions calculated by an unverified country pack — compliance not "
                                 "verified"]
    assert content["employee"]["name"].startswith("Emma") and content["currency"] == "INR"
    data, rerendered = document_pdf(w["cid"], doc_id)
    assert not rerendered and hashlib.sha256(data).hexdigest() == sha
    text_ = pdf_text(data)
    assert "₹" in text_ and "Net pay" in text_ and number in text_
    assert "RETENTION_ACCRUAL" not in text_ and "999.00" not in text_
    assert "■" not in text_


def test_i15_label_when_no_statutory_lines_and_fixture_badge(docs_world):
    """I-15: an entity without a verified pack is labelled; I-07: fixture lines are badged."""
    w = docs_world
    plain = (("BASIC", "EARNING", "40000.00", {}),)
    run_id, rows = emma_run(w, lines=plain)
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert content["labels"] == ["Statutory deductions not calculated — compliance not verified"]
    data, _ = document_pdf(w["cid"], payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert "Statutory deductions not calculated — compliance not verified" in " ".join(pdf_text(data).split())
    assert "" not in pdf_text(data) and "■" not in pdf_text(data)  # no missing glyphs
    fixture = (("BASIC", "EARNING", "40000.00", {}), ("PF_EMPLOYEE", "DEDUCTION", "1800.00",
                                                       {"statutory": True, "fixture": True}))
    run_id, rows = emma_run(w, start=date(2026, 10, 1), lines=fixture)
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert "FIXTURE — not law" in content["labels"]
    assert "Statutory deductions not calculated — compliance not verified" not in content["labels"]


def test_employer_contributions_are_separate_and_never_in_net(docs_world):
    w = docs_world
    set_setting(w["cid"], "payslip.show_employer_contributions", True)
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    content = content_of(doc_id)
    assert [i["code"] for i in content["employer_contributions"]] == ["EMPLOYER_PF"]
    assert content["employer_note"] == "Paid by your employer — not part of your take-home pay"
    assert content["totals"]["net"] == "68700.00"
    assert "not part of your take-home pay" in pdf_text(document_pdf(w["cid"], doc_id)[0])


def test_ytd_column_sums_finalized_runs_of_the_tax_year(docs_world):
    w = docs_world
    first, _ = emma_run(w, start=date(2026, 8, 1))
    run_id, rows = emma_run(w, start=date(2026, 9, 1))
    generate(w["cid"], first)
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    basic = next(i for i in content["earnings"] if i["code"] == "BASIC")
    assert basic["amount"] == "50000.00" and basic["ytd"] == "100000.00"
    set_setting(w["cid"], "payslip.show_ytd", False)
    run_id, rows = emma_run(w, start=date(2026, 10, 1))
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert content["show_ytd"] is False and "ytd" not in content["earnings"][0]


def test_payment_status_comes_from_evidence_only(docs_world):
    """3.6 §3 #9 and I-17: a submitted manual upload is "unverified"; "Paid on" needs verified settlement."""
    w = docs_world
    set_setting(w["cid"], "payslip.publish_trigger", "ON_SETTLED")
    run_id, rows = emma_run(w)
    emma_row, ivan_row = rows[w["emp"]["Emma"]["id"]], rows[w["emp"]["Ivan"]["id"]]
    for row_id, employee_id, status, channel, evidence in (
            (emma_row, w["emp"]["Emma"]["id"], "SUBMITTED", "MANUAL_UPLOAD", "UNVERIFIED"),
            (ivan_row, w["emp"]["Ivan"]["id"], "SETTLED", "API", "VERIFIED")):
        add_instruction(w["cid"], run_id, row_id, employee_id, w["le01"], w["group"], status=status, channel=channel,
                        evidence=evidence)
    generate(w["cid"], run_id)
    emma = content_of(payslip_of(emma_row)[0])["payment"]
    ivan = content_of(payslip_of(ivan_row)[0])["payment"]
    assert emma["text"] == "Submitted for manual payment — unverified" and emma["reference"].startswith("••••")
    assert ivan["key"] == "PAID" and ivan["text"].startswith("Paid on ")
    # ON_SETTLED: only the verified settlement is published (EC-DOC-11 in test_publication.py).
    assert payslip_of(emma_row)[1] == "GENERATED" and payslip_of(ivan_row)[1] == "PUBLISHED"


def add_instruction(cid, run_id, run_employee_id, employee_id, entity, group, *, status="SUBMITTED",
                    channel="MANUAL_UPLOAD", evidence="UNVERIFIED", amount="68700.00"):
    obligation = sql(
        "INSERT INTO payment_obligations (company_id, source_type, run_id, run_employee_id, payee_type, employee_id, "
        "legal_entity_id, pay_group_id, amount, currency, status, row_version, created_at, updated_at) VALUES (:c, "
        "'RUN_EMPLOYEE', :r, :re, 'EMPLOYEE', :e, :le, :g, :a, 'INR', 'OPEN', 1, now(), now()) RETURNING id",
        c=cid, r=run_id, re=run_employee_id, e=employee_id, le=entity, g=group, a=Decimal(amount))[0][0]
    settled = status in ("SETTLED", "RECONCILED")
    return sql(
        "INSERT INTO payment_instructions (company_id, obligation_id, run_id, employee_id, legal_entity_id, sequence, "
        "idempotency_key, reference, beneficiary_snapshot, amount, currency, status, submission_channel, "
        "evidence_status, evidence_source, submitted_at, settled_at, settled_amount, row_version, created_at, "
        "updated_at) VALUES (:c, :o, :r, :e, :le, 1, :k, :ref, CAST(:snap AS jsonb), :a, 'INR', :s, :ch, :ev, :src, "
        "now(), :st, :sa, 1, now(), now()) RETURNING id",
        c=cid, o=obligation, r=run_id, e=employee_id, le=entity, k=f"test:{run_employee_id}",
        ref=f"PAY{run_employee_id:08d}7781", snap='{"account_masked": "••••4321", "bank_name": "HDFC Bank", '
                                                   '"holder_name_masked": "E••• T•••"}',
        a=Decimal(amount), s=status, ch=channel, ev=evidence, src="STATEMENT" if evidence == "VERIFIED" else None,
        st=date(2026, 9, 30) if settled else None, sa=Decimal(amount) if settled else None)[0][0]


def test_bank_account_is_masked_on_the_payslip(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    add_instruction(w["cid"], run_id, rows[w["emp"]["Emma"]["id"]], w["emp"]["Emma"]["id"], w["le01"], w["group"])
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert content["employee"]["bank"] == "HDFC Bank ••••4321"
    assert not re.search(r"\d{6,}", content["employee"]["bank"])


# ---------------------------------------------------------------------------
# Immutability and re-render (EC-DOC-03)
# ---------------------------------------------------------------------------
def test_ec_doc_03_rerender_reproduces_bytes_and_issued_documents_never_change(docs_world):
    """EC-COR-15: after a template change the issued payslip serves its stored bytes and keeps its lineage."""
    from app.core import storage

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id, _, number, _, sha, path, _ = payslip_of(rows[w["emp"]["Emma"]["id"]])
    original, _ = document_pdf(w["cid"], doc_id)
    # A template change after issue does not touch the issued payslip: it keeps its frozen template and brand.
    from tests.payroll.test_wage_inputs import activate_policy

    from app.payroll import policy_types as pt

    activate_policy(w["acme"], "PAYSLIP_TEMPLATE", {**pt.validate_fields("PAYSLIP_TEMPLATE", {}), "layout": "COMPACT",
                                                    "footer_note": "Changed after issue"})
    assert document_pdf(w["cid"], doc_id) == (original, False)
    assert content_of(doc_id)["template"]["layout"] == "CLASSIC"
    # The stored file is lost: the document is re-rendered from the same content, template and brand version.
    storage.delete(w["cid"], path)
    data, rerendered = document_pdf(w["cid"], doc_id)
    assert rerendered and hashlib.sha256(data).hexdigest() == sha and data == original
    flags = sql("SELECT rerendered, rerender_mismatch FROM documents WHERE id = :d", d=doc_id)[0]
    assert flags == (True, False)
    actions = [a for (a,) in sql("SELECT action FROM document_access_log WHERE document_id = :d", d=doc_id)]
    assert "REGENERATE" in actions
    with tenant(w["cid"]) as conn:  # identity, content and hash are frozen by trigger
        expect_error(conn, CHECK, "UPDATE documents SET content = '{\"x\": 1}'::jsonb WHERE id = :d", d=doc_id)
        expect_error(conn, CHECK, "UPDATE documents SET sha256 = repeat('0', 64) WHERE id = :d", d=doc_id)
        expect_error(conn, CHECK, "UPDATE documents SET number = 'X' WHERE id = :d", d=doc_id)
        expect_error(conn, CHECK, "DELETE FROM documents WHERE id = :d", d=doc_id)
        expect_error(conn, CHECK, "UPDATE document_access_log SET action = 'VIEW' WHERE document_id = :d", d=doc_id)


def test_layouts_render_classic_modern_compact(docs_world):
    from app.payroll.services import pdf_render

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    hashes = set()
    for layout in ("CLASSIC", "MODERN", "COMPACT"):
        variant = {**content, "template": {**content["template"], "layout": layout}}
        data = pdf_render.render(variant)
        assert data.startswith(b"%PDF") and pdf_render.render(variant) == data  # deterministic
        assert "Net pay" in pdf_text(data)
        hashes.add(hashlib.sha256(data).hexdigest())
    assert len(hashes) == 3


def test_payslip_template_policy_controls_layout_and_footer(docs_world):
    from tests.payroll.test_wage_inputs import activate_policy

    from app.payroll import policy_types as pt

    w = docs_world
    fields = pt.validate_fields("PAYSLIP_TEMPLATE", {})
    fields = {**fields, "layout": "MODERN", "footer_note": "Questions? payroll@acme.test"}
    activate_policy(w["acme"], "PAYSLIP_TEMPLATE", fields)
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    content = content_of(doc_id)
    assert content["template"]["layout"] == "MODERN"
    template_version = sql("SELECT template_version, template_policy_version_id FROM documents WHERE id = :d",
                           d=doc_id)[0]
    assert template_version[0].startswith("PAYSLIP_TEMPLATE:") and template_version[1] is not None
    assert "Questions? payroll@acme.test" in pdf_text(document_pdf(w["cid"], doc_id)[0])


# ---------------------------------------------------------------------------
# Languages and fonts (EC-DOC-13, EC-DOC-23)
# ---------------------------------------------------------------------------
def test_ec_doc_13_bilingual_right_to_left_payslip(docs_world):
    w = docs_world
    set_setting(w["cid"], "payslip.languages", ["ar", "en"])
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    content = content_of(doc_id)
    assert content["languages"] == ["ar", "en"]
    data, _ = document_pdf(w["cid"], doc_id)
    fonts = pdf_fonts(data)
    assert any("NotoSansArabic" in f for f in fonts) and any(f.endswith("NotoSans-Regular") or "NotoSans" in f
                                                              for f in fonts)
    text_ = pdf_text(data)
    assert "50,000.00" in text_  # numbers stay left-to-right with Latin digits
    assert "Net pay" in text_


def test_ec_doc_23_rupee_dirham_and_devanagari_round_trip():
    from app.payroll.services import pdf_render

    content = {
        "kind": "PAYSLIP", "currency": "INR", "languages": ["en"], "locale": "en-IN",
        "template": {"layout": "CLASSIC", "sections": ["HEADER", "EMPLOYEE", "EARNINGS", "DEDUCTIONS", "TOTALS",
                                                       "FOOTER"]},
        "employer": {"legal_name": "Acme India Pvt Ltd"}, "brand": {"display_name": "Acme"},
        "employee": {"name": "अमित शर्मा", "code": "E0001"}, "period": {"label": "September 2026"},
        "earnings": [{"code": "BASIC", "label": "Basic", "amount": "1234567.89"}], "deductions": [],
        "totals": {"gross": "1234567.89", "deductions": "0.00", "net": "1234567.89",
                   "net_words": pdf_render.amount_in_words("1234567.89", "INR", "en-IN")},
        "number": "PS/IN01/2026-27/000001", "issue_date": "2026-09-30",
    }
    text_ = pdf_text(pdf_render.render(content))
    assert "₹12,34,567.89" in text_ and "अमित" in text_.replace("‍", "")
    assert "Twelve lakh thirty-four thousand five hundred sixty-seven" in content["totals"]["net_words"]
    aed = pdf_text(pdf_render.render({**content, "currency": "AED", "locale": "en-AE"}))
    assert "AED 1,234,567.89" in aed
    assert pdf_render.format_money("12.345", "KWD") == "KWD 12.345" and pdf_render.format_money("1500", "JPY") == "¥1,500"


def test_ec_doc_22_payslips_never_use_compact_indian_notation():
    """₹4.2L belongs to KPI cards only: payslip amounts, totals and words are always written out in full."""
    from app.payroll.services import pdf_render

    assert pdf_render.format_money("420000", "INR", "en-IN") == "₹4,20,000.00"
    assert pdf_render.format_amount("12500000.5", "INR", "en-IN") == "1,25,00,000.50"
    content = {
        "kind": "PAYSLIP", "currency": "INR", "languages": ["en"], "locale": "en-IN",
        "template": {"layout": "COMPACT", "sections": ["HEADER", "EMPLOYEE", "EARNINGS", "DEDUCTIONS", "TOTALS",
                                                       "FOOTER"]},
        "employer": {"legal_name": "Acme India Pvt Ltd"}, "brand": {"display_name": "Acme"},
        "employee": {"name": "Asha Rao", "code": "E0002"}, "period": {"label": "September 2026"},
        "earnings": [{"code": "BASIC", "label": "Basic", "amount": "420000.00"}], "deductions": [],
        "totals": {"gross": "420000.00", "deductions": "0.00", "net": "420000.00",
                   "net_words": pdf_render.amount_in_words("420000.00", "INR", "en-IN")},
        "number": "PS/IN01/2026-27/000002", "issue_date": "2026-09-30",
    }
    text_ = pdf_text(pdf_render.render(content))
    assert "4,20,000.00" in text_ and "Four lakh twenty thousand" in content["totals"]["net_words"]
    assert not re.search(r"\d(\.\d+)?\s?(L|Cr|K|lakh|crore)\b", text_)


# ---------------------------------------------------------------------------
# v2 namespace (I-19)
# ---------------------------------------------------------------------------
def test_i19_v2_payslips_never_collide_with_legacy_ids(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    row_id = rows[w["emp"]["Emma"]["id"]]
    doc_type, source_key = sql("SELECT doc_type, source_key FROM documents WHERE run_employee_id = :r",
                               r=row_id)[0]
    assert (doc_type, source_key) == ("PAYSLIP_V2", f"run_employee:{row_id}")
    assert sql("SELECT count(*) FROM issued_documents")[0][0] == 0
    # A legacy payroll id equal to the run-employee id never serves the v2 payslip.
    r = w["acme"].get(f"/api/v1/payroll/{row_id}/slip.pdf")
    assert r.status_code == 404
