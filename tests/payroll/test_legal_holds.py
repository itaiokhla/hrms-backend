"""Legal holds and document retention (EC-SEC-16, 3.6 document retention, 09 §6; FOLLOW-UPS F-73).

Retention date at issue (company policy), the sweep with and without holds in every scope, stacked holds,
hand-checked counts, idempotent and bounded sweeps, the periodic job, the database guards (a held document is
never redacted or deleted, even by SQL), downloads and exports of held documents, employee erasure requests
refused with LEGAL_HOLD, permission / step-up / maker-checker, and three-tenant isolation.

Fixture world (``docs_world``): Acme with LE01 (Emma, Ivan) and AE01, Beta and Cobalt as minimal tenants.
Documents of the scenarios: payslips of Emma and Ivan for September 2026 and for October 2026 (a later run),
the September register and the September funding request (a type retention never deletes). The September
documents and the funding request are aged (retention date in the past); the October payslips are not.
"""

import io
import json
import zipfile
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import storage
from app.core.errors import AppError
from tests.helpers import create_employee
from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    ME,
    PAY,
    docs_world,
    emma_run,
    finalized_run,
    generate,
    payslip_of,
)
from tests.payroll.test_wage_inputs import (
    CHECK,
    FOREIGN_KEY,
    audit_actions,
    expect_error,
    role,
    session,
    sql,
    tenant,
    today_of,
    user_with_role,
)

HOLDS = f"{PAY}/legal-holds"
MANAGE = "payroll:legal_hold:manage"
COUNT_KEYS = {"as_of", "trigger", "due", "held", "redacted", "remaining", "not_due", "no_retention_date", "more"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def step_up(client, minutes_ago=0):
    sql("UPDATE user_sessions SET step_up_at = now() - make_interval(mins => :m), step_up_method = 'EMAIL_OTP' "
        "WHERE user_id = :u", m=minutes_ago, u=client.user_id)


def officers(app, admin, prefix="Lena", count=2, role_name="Legal hold officers"):
    """``count`` users holding payroll:legal_hold:manage at COMPANY scope (a custom role: no default role has it)."""
    role_id = role(admin, role_name, "COMPANY", [MANAGE])
    return [user_with_role(app, admin, create_employee(admin, f"{prefix}{i}", hire_date="2024-01-15"), role_id)
            for i in range(count)]


def storage_path(document_id):
    return sql("SELECT storage_path FROM documents WHERE id = :d", d=document_id)[0][0]


def file_exists(cid, path):
    try:
        return storage.resolve(cid, path).is_file()
    except AppError:  # resolve() answers 404 for a missing file
        return False


def age(*document_ids):
    sql("UPDATE documents SET retention_until = current_date - 30 WHERE id = ANY(:ids)", ids=list(document_ids))


def redacted(document_id):
    return sql("SELECT retention_redacted_at FROM documents WHERE id = :d", d=document_id)[0][0] is not None


def scenario(w):
    """Documents of Acme (see the module docstring); the September ones and the funding request are aged."""
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    ids = {"emma": payslip_of(rows[emma])[0], "ivan": payslip_of(rows[ivan])[0]}
    r = w["acme"].post(f"{PAY}/runs/{run_id}/documents/register")
    assert r.status_code == 201, r.text
    ids["register"] = r.json()["id"]
    r = w["acme"].post(f"{PAY}/runs/{run_id}/documents/funding-request")
    assert r.status_code == 201, r.text
    ids["funding"] = r.json()["id"]
    later_run, later_rows = emma_run(w, start=date(2026, 10, 1))
    generate(w["cid"], later_run)
    ids["emma_oct"] = payslip_of(later_rows[emma])[0]
    ids["ivan_oct"] = payslip_of(later_rows[ivan])[0]
    age(ids["emma"], ids["ivan"], ids["register"], ids["funding"])
    ids["paths"] = {k: storage_path(v) for k, v in ids.items() if k != "paths"}
    ids["run_id"] = run_id
    return ids


def sweep_counts(client):
    r = client.post(f"{HOLDS}/sweeps")
    assert r.status_code == 200, r.text
    assert set(r.json()) == COUNT_KEYS
    return r.json()


def place(client, **body):
    r = client.post(HOLDS, json={"reason": "Pending litigation", **body})
    assert r.status_code == 201, r.text
    return r.json()


def sweep_audit_details(cid):
    return [d for (d,) in sql("SELECT details FROM audit_logs WHERE company_id = :c AND module = 'Legal Holds' "
                              "AND action = 'RETENTION_SWEEP' ORDER BY id", c=cid)]


# ---------------------------------------------------------------------------
# Retention date at issue
# ---------------------------------------------------------------------------
def test_documents_get_a_retention_date_from_the_company_policy(docs_world):
    """Default 8 years from the issue date; the company's own setting changes later documents only."""
    from app.payroll.services.legal_holds import add_years

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    issue_date, until = sql("SELECT issue_date, retention_until FROM documents WHERE id = :d", d=doc_id)[0]
    assert until == add_years(issue_date, 8)
    sql("DELETE FROM setting_values WHERE company_id = :c AND key = 'privacy.payroll_retention_years'", c=w["cid"])
    sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
        "row_version, source, created_at, updated_at) VALUES (:c, 'privacy.payroll_retention_years', 'TENANT', NULL, "
        "'10'::jsonb, false, '2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=w["cid"])
    later, later_rows = emma_run(w, start=date(2026, 10, 1))
    generate(w["cid"], later)
    other = payslip_of(later_rows[w["emp"]["Emma"]["id"]])[0]
    issue_date2, until2 = sql("SELECT issue_date, retention_until FROM documents WHERE id = :d", d=other)[0]
    assert until2 == add_years(issue_date2, 10)
    assert sql("SELECT retention_until FROM documents WHERE id = :d", d=doc_id)[0][0] == until  # not rewritten
    assert add_years(date(2024, 2, 29), 1) == date(2025, 2, 28)


# ---------------------------------------------------------------------------
# The sweep without holds
# ---------------------------------------------------------------------------
def test_sweep_deletes_only_due_stored_payslips_and_registers_hand_checked(app, docs_world):
    """EC-SEC-16: without a hold the sweep deletes exactly the due payslip PDFs and registers (hand-counted)."""
    w = docs_world
    ids = scenario(w)
    a, _ = officers(app, w["acme"])
    step_up(a)
    run_before = sql("SELECT status, row_version FROM payroll_runs WHERE id = :r", r=ids["run_id"])[0]
    lines_before = sql("SELECT count(*), coalesce(sum(amount), 0) FROM payroll_lines WHERE company_id = :c",
                       c=w["cid"])[0]
    hashes = {k: sql("SELECT sha256, content_hash, number FROM documents WHERE id = :d", d=ids[k])[0]
              for k in ("emma", "ivan", "register")}
    assert all(file_exists(w["cid"], p) for p in ids["paths"].values())
    summary = a.get(f"{HOLDS}/summary").json()
    # September payslips (2) and register (1) are due; the aged funding request is a type retention never deletes.
    assert summary["documents_due"] == 3 and summary["documents_due_deletable"] == 3
    assert summary["documents_redacted"] == 0 and summary["active_holds"] == 0
    counts = sweep_counts(a)
    assert counts["due"] == 3 and counts["held"] == 0 and counts["redacted"] == 3 and counts["remaining"] == 0
    assert counts["not_due"] == 2 and counts["no_retention_date"] == 0 and counts["more"] is False
    assert counts["trigger"] == "MANUAL" and counts["as_of"] == today_of(w["cid"]).isoformat()
    for key in ("emma", "ivan", "register"):
        assert redacted(ids[key]) and storage_path(ids[key]) is None
        assert not file_exists(w["cid"], ids["paths"][key])
        assert sql("SELECT sha256, content_hash, number FROM documents WHERE id = :d", d=ids[key])[0] == hashes[key]
    for key in ("emma_oct", "ivan_oct", "funding"):  # not due / not a swept type: untouched
        assert not redacted(ids[key]) and file_exists(w["cid"], ids["paths"][key])
    # Finalized financial records are never touched.
    assert sql("SELECT status, row_version FROM payroll_runs WHERE id = :r", r=ids["run_id"])[0] == run_before
    assert sql("SELECT count(*), coalesce(sum(amount), 0) FROM payroll_lines WHERE company_id = :c",
               c=w["cid"])[0] == lines_before
    # The redacted payslip is never re-rendered or served again: 410, no PDF.
    emma = w["logins"]["Emma"]
    link = emma.get(f"{ME}/payslips/{ids['emma']}/download")
    served = emma.get(link.json()["url"]) if link.status_code == 200 else link
    assert served.status_code == 410 and served.json()["code"] == "RETENTION_EXPIRED"
    assert not file_exists(w["cid"], ids["paths"]["emma"])  # nothing was re-stored
    assert w["acme"].get(f"{PAY}/payslips/{ids['emma']}").json()["retention_redacted_at"] is not None
    # Idempotent: a second sweep finds nothing left (and is audited too).
    again = sweep_counts(a)
    assert again["due"] == 0 and again["redacted"] == 0 and again["held"] == 0 and again["not_due"] == 2
    summary = a.get(f"{HOLDS}/summary").json()
    assert summary["documents_redacted"] == 3 and summary["documents_due"] == 0
    assert summary["last_sweep"]["redacted"] == 0
    sweeps = a.get(f"{HOLDS}/sweeps").json()["items"]
    assert [s["redacted"] for s in sweeps] == [0, 3]


def test_sweep_audit_carries_counts_only(app, docs_world):
    w = docs_world
    ids = scenario(w)
    a, _ = officers(app, w["acme"])
    step_up(a)
    sweep_counts(a)
    [details] = sweep_audit_details(w["cid"])
    assert set(details) == COUNT_KEYS
    assert (details["due"], details["held"], details["redacted"], details["not_due"]) == (3, 0, 3, 2)
    blob = json.dumps(details)
    assert "Emma" not in blob and "payroll-documents" not in blob and ".pdf" not in blob
    description = sql("SELECT description FROM audit_logs WHERE company_id = :c AND action = 'RETENTION_SWEEP'",
                      c=w["cid"])[0][0]
    assert "Emma" not in description and "3 stored file(s) deleted, 0 held back" in description


def test_sweep_is_bounded_and_continues_until_nothing_is_due(docs_world):
    from app.payroll.services import legal_holds

    w = docs_world
    ids = scenario(w)
    results = []
    for _ in range(4):
        with session(w["cid"]) as (db, _):
            results.append(legal_holds.sweep(db, w["cid"], limit=1))
            db.commit()
    assert [(r["redacted"], r["remaining"], r["more"]) for r in results] == [
        (1, 2, True), (1, 1, True), (1, 0, False), (0, 0, False)]
    assert sum(redacted(ids[k]) for k in ("emma", "ivan", "register")) == 3


def test_sweep_rolls_back_when_a_file_cannot_be_removed(docs_world, monkeypatch):
    """A storage failure never leaves a document marked redacted while its file remains."""
    from app.payroll.services import legal_holds

    w = docs_world
    ids = scenario(w)

    def broken(company_id, path):
        raise OSError("storage unavailable")

    monkeypatch.setattr(legal_holds, "_remove_file", broken)
    with session(w["cid"]) as (db, _):
        with pytest.raises(OSError):
            legal_holds.sweep(db, w["cid"])
        db.rollback()
    assert not any(redacted(ids[k]) for k in ("emma", "ivan", "register"))
    assert all(file_exists(w["cid"], ids["paths"][k]) for k in ("emma", "ivan", "register"))


# ---------------------------------------------------------------------------
# Scoped holds
# ---------------------------------------------------------------------------
# name -> (hold body builder, documents the hold covers (hand counted), due, held, redacted, which stay)
SCOPED = {
    "employee": (lambda w, i: {"scope": "EMPLOYEE", "employee_id": w["emp"]["Emma"]["id"]}, 4, 3, 2, 1,
                 {"emma", "register"}),
    "legal_entity": (lambda w, i: {"scope": "LEGAL_ENTITY", "legal_entity_id": w["le01"]}, 6, 3, 3, 0,
                     {"emma", "ivan", "register"}),
    "other_entity": (lambda w, i: {"scope": "LEGAL_ENTITY", "legal_entity_id": w["ae"]}, 0, 3, 0, 3, set()),
    "payslips": (lambda w, i: {"scope": "DOCUMENT_TYPE", "doc_type": "PAYSLIP_V2"}, 4, 3, 2, 1, {"emma", "ivan"}),
    "registers": (lambda w, i: {"scope": "DOCUMENT_TYPE", "doc_type": "PAYROLL_REGISTER"}, 1, 3, 1, 2,
                  {"register"}),
    "one_document": (lambda w, i: {"scope": "DOCUMENT", "document_id": i["ivan"]}, 1, 3, 1, 2, {"ivan"}),
    "unswept_type": (lambda w, i: {"scope": "DOCUMENT_TYPE", "doc_type": "FUNDING_REQUEST"}, 1, 3, 0, 3, set()),
}


@pytest.mark.parametrize("name", list(SCOPED))
def test_sweep_skips_documents_under_a_hold_of_every_scope(app, docs_world, name):
    """EC-SEC-16: a hold of any scope makes the sweep skip the documents it covers (hand-counted per scope)."""
    w = docs_world
    ids = scenario(w)
    build, covered, due, held, deleted, stays = SCOPED[name]
    a, _ = officers(app, w["acme"])
    step_up(a)
    hold = place(a, **build(w, ids))
    assert hold["status"] == "ACTIVE" and hold["documents_covered"] == covered
    counts = sweep_counts(a)
    assert (counts["due"], counts["held"], counts["redacted"], counts["remaining"]) == (due, held, deleted, 0)
    for key in ("emma", "ivan", "register"):
        assert redacted(ids[key]) == (key not in stays), key
        assert file_exists(w["cid"], ids["paths"][key]) == (key in stays), key
    assert file_exists(w["cid"], ids["paths"]["funding"])  # never a swept type
    assert redacted(ids["emma_oct"]) is False
    assert a.get(f"{HOLDS}/summary").json()["documents_under_hold"] == covered


def test_stacked_holds_protect_until_the_last_one_is_released(app, docs_world):
    """EC-SEC-16: stacked holds protect a document until the last one is released."""
    w = docs_world
    ids = scenario(w)
    a, b = officers(app, w["acme"])
    first = place(a, scope="DOCUMENT", document_id=ids["emma"], reference="CASE-1")
    second = place(a, scope="EMPLOYEE", employee_id=w["emp"]["Emma"]["id"], reference="CASE-2")
    step_up(a), step_up(b)
    first_sweep = sweep_counts(b)  # Emma's payslip and the register (employee hold) are held back; Ivan's goes
    assert (first_sweep["due"], first_sweep["held"], first_sweep["redacted"]) == (3, 2, 1)
    assert redacted(ids["ivan"])
    assert b.post(f"{HOLDS}/{first['id']}/release", json={"reason": "Case 1 closed"}).status_code == 200
    assert sweep_counts(b)["redacted"] == 0  # the second hold still covers both
    assert redacted(ids["emma"]) is False
    assert b.post(f"{HOLDS}/{second['id']}/release", json={"reason": "Case 2 closed"}).status_code == 200
    counts = sweep_counts(b)
    assert (counts["due"], counts["held"], counts["redacted"]) == (2, 0, 2)  # Emma and the register
    assert redacted(ids["emma"]) and redacted(ids["register"])
    assert audit_actions(w["cid"], "legal_hold", first["id"]) == ["LEGAL_HOLD_CREATED", "LEGAL_HOLD_RELEASED"]


def test_held_documents_download_and_export_normally(app, docs_world):
    """EC-SEC-16: exports and downloads of held documents keep working."""
    w = docs_world
    ids = scenario(w)
    a, _ = officers(app, w["acme"])
    place(a, scope="EMPLOYEE", employee_id=w["emp"]["Emma"]["id"])
    step_up(a)
    assert sweep_counts(a)["redacted"] == 1  # Ivan's September payslip only
    emma, ivan = w["logins"]["Emma"], w["logins"]["Ivan"]
    link = emma.get(f"{ME}/payslips/{ids['emma']}/download")
    assert link.status_code == 200
    served = emma.get(link.json()["url"])
    assert served.status_code == 200 and served.content.startswith(b"%PDF")  # the held payslip still downloads
    assert sql("SELECT count(*) FROM document_access_log WHERE document_id = :d AND action = 'DOWNLOAD'",
               d=ids["emma"])[0][0] == 1
    assert w["acme"].get(f"{PAY}/payslips/{ids['emma']}").json()["retention_redacted_at"] is None
    # Staff export of the run: the held payslip is in the ZIP, the redacted one is skipped (not an error).
    r = w["acme"].post(f"{PAY}/payslips/zip", json={"run_id": ids["run_id"]})
    assert r.status_code == 202, r.text
    export = w["acme"].get(f"{PAY}/exports/{r.json()['id']}").json()
    assert export["status"] == "READY" and export["item_count"] == 1, export
    archive = zipfile.ZipFile(io.BytesIO(w["acme"].get(w["acme"].post(f"{PAY}/exports/{export['id']}/download")
                                                      .json()["url"]).content))
    assert len([n for n in archive.namelist() if n.endswith(".pdf")]) == 1
    # The redacted payslip is gone for its owner (410), the held one is not.
    ivan_link = ivan.get(f"{ME}/payslips/{ids['ivan']}/download")
    ivan_served = ivan.get(ivan_link.json()["url"]) if ivan_link.status_code == 200 else ivan_link
    assert ivan_served.status_code == 410


# ---------------------------------------------------------------------------
# The database guards (the sweep, SQL and services all meet the same rule)
# ---------------------------------------------------------------------------
def _refused(cid, statement, expected, **params):
    with tenant(cid) as conn:
        with pytest.raises(DBAPIError) as info:
            with conn.begin_nested():
                conn.execute(text(statement), params)
    assert info.value.orig.pgcode == CHECK, info.value.orig
    assert expected in str(info.value.orig), info.value.orig


REDACT = "UPDATE documents SET retention_redacted_at = now(), storage_path = NULL WHERE id = :d"


def test_database_refuses_to_redact_or_delete_a_held_document(app, docs_world):
    """EC-SEC-16: the database itself refuses to redact or delete a held document."""
    w = docs_world
    ids = scenario(w)
    a, _ = officers(app, w["acme"])
    place(a, scope="DOCUMENT", document_id=ids["emma"])
    cid = w["cid"]
    _refused(cid, REDACT, "LEGAL_HOLD", d=ids["emma"])  # held: refused even though aged and a swept type
    _refused(cid, "DELETE FROM documents WHERE id = :d", "DOCUMENT_IMMUTABLE", d=ids["emma"])
    _refused(cid, "DELETE FROM documents WHERE id = :d", "DOCUMENT_IMMUTABLE", d=ids["ivan"])  # held or not
    assert redacted(ids["emma"]) is False and file_exists(cid, ids["paths"]["emma"])
    # Not held but not yet due, wrong type, no retention date: refused too.
    _refused(cid, REDACT, "RETENTION_NOT_DUE", d=ids["ivan_oct"])
    _refused(cid, REDACT, "RETENTION_TYPE", d=ids["funding"])
    sql("UPDATE documents SET retention_until = NULL WHERE id = :d", d=ids["ivan"])
    _refused(cid, REDACT, "RETENTION_NOT_DUE", d=ids["ivan"])
    sql("UPDATE documents SET retention_until = current_date - 1 WHERE id = :d", d=ids["ivan"])
    _refused(cid, "UPDATE documents SET retention_redacted_at = now() WHERE id = :d", "RETENTION_REDACTED",
             d=ids["ivan"])  # the stored file pointer must go with the redaction
    # An allowed redaction is final: it is never undone and the file pointer never restored.
    with tenant(cid) as conn:
        conn.execute(text(REDACT), {"d": ids["ivan"]})
    _refused(cid, "UPDATE documents SET retention_redacted_at = NULL WHERE id = :d", "RETENTION_REDACTED",
             d=ids["ivan"])
    _refused(cid, "UPDATE documents SET storage_path = 'x/y.pdf' WHERE id = :d", "RETENTION_REDACTED", d=ids["ivan"])


def test_service_guards_and_release_reopens_the_documents(app, docs_world):
    from app.core.errors import AppError
    from app.models.pay_documents import PayDocument
    from app.payroll.services import documents, legal_holds

    w = docs_world
    ids = scenario(w)
    a, b = officers(app, w["acme"])
    hold = place(a, scope="LEGAL_ENTITY", legal_entity_id=w["le01"])
    with session(w["cid"]) as (db, _):
        held_doc = db.get(PayDocument, ids["emma"])
        with pytest.raises(AppError) as info:
            documents.ensure_deletable(db, held_doc)
        assert info.value.status_code == 409 and info.value.code == "LEGAL_HOLD"
        assert legal_holds.document_hold_ids(db, w["cid"], ids["emma"]) == [hold["id"]]
        with pytest.raises(AppError) as erase:
            legal_holds.ensure_employee_not_held(db, w["cid"], w["emp"]["Ivan"]["id"])
        assert erase.value.code == "LEGAL_HOLD"
    step_up(b)
    assert b.post(f"{HOLDS}/{hold['id']}/release", json={"reason": "Matter closed"}).status_code == 200
    with session(w["cid"]) as (db, _):
        documents.ensure_deletable(db, db.get(PayDocument, ids["emma"]))  # no hold any more
        legal_holds.ensure_employee_not_held(db, w["cid"], w["emp"]["Ivan"]["id"])


# ---------------------------------------------------------------------------
# Employee erasure requests (EC-SEC-16)
# ---------------------------------------------------------------------------
def erase(client, employee_id, reason="Subject access request"):
    return client.post(f"{HOLDS}/erasure-requests", json={"employee_id": employee_id, "reason": reason})


def test_erasure_request_is_refused_on_held_data_and_otherwise_keeps_the_retention_period(app, docs_world):
    """EC-SEC-16: erasure requests on held data are refused with LEGAL_HOLD."""
    w = docs_world
    ids = scenario(w)
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    a, b = officers(app, w["acme"])
    step_up(a)
    # A hold on one of Emma's documents refuses her whole request; Ivan's is unaffected by it.
    place(a, scope="DOCUMENT", document_id=ids["emma_oct"])
    r = erase(a, emma)
    assert r.status_code == 409 and r.json()["code"] == "LEGAL_HOLD"
    assert not redacted(ids["emma"]) and file_exists(w["cid"], ids["paths"]["emma"])
    assert audit_actions(w["cid"], "employee", emma)[-1] == "RETENTION_ERASURE_REFUSED"  # the refusal is kept
    # Ivan: the aged September payslip goes; the October payslip stays until its retention ends.
    r = erase(a, ivan)
    assert r.status_code == 200, r.text
    assert r.json() == {"employee_id": ivan, "redacted": 1, "kept_until_retention_ends": 1, "more": False}
    assert redacted(ids["ivan"]) and not redacted(ids["ivan_oct"]) and not redacted(ids["emma"])
    assert not redacted(ids["register"])  # erasure deals with the employee's own payslips only
    # An employee hold refuses even without any document being held.
    sql("UPDATE documents SET retention_until = current_date - 5 WHERE id = :d", d=ids["ivan_oct"])
    hold = place(a, scope="EMPLOYEE", employee_id=ivan)
    r = erase(a, ivan)
    assert r.status_code == 409 and r.json()["code"] == "LEGAL_HOLD" and not redacted(ids["ivan_oct"])
    step_up(b)
    assert b.post(f"{HOLDS}/{hold['id']}/release", json={"reason": "Closed"}).status_code == 200
    r = erase(a, ivan)
    assert r.status_code == 200 and r.json()["redacted"] == 1 and redacted(ids["ivan_oct"])
    # Counts only in the audit trail.
    erasures = [d for (d,) in sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = "
                                  "'RETENTION_ERASURE' ORDER BY id", c=w["cid"])]
    assert [e["trigger"] for e in erasures] == ["ERASURE", "ERASURE"] and set(erasures[0]) == COUNT_KEYS
    # Negatives: step-up, permission, validation, unknown employee.
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=a.user_id)
    stale = erase(a, ivan)
    assert stale.status_code == 403 and stale.json()["code"] == "STEP_UP_REQUIRED"
    assert erase(w["acme"], ivan).status_code == 403  # the founder is not a legal hold manager
    step_up(a)
    assert erase(a, 999999).status_code == 404
    assert a.post(f"{HOLDS}/erasure-requests", json={"employee_id": ivan, "reason": " "}).status_code == 422
    assert a.post(f"{HOLDS}/erasure-requests", json={"employee_id": ivan, "reason": "x", "company_id": 9}
                  ).status_code == 422


# ---------------------------------------------------------------------------
# Permissions, step-up, maker-checker, validation
# ---------------------------------------------------------------------------
def test_permission_registry_entry():
    from app.core import permission_registry as reg

    definition = next(p for p in reg.PERMISSIONS if p.code == MANAGE)
    assert definition.scopes == (reg.Scope.COMPANY,) and definition.payroll_confidential
    assert definition.full_access_scopes == ()  # a company administrator does not hold it implicitly
    assert all(MANAGE not in {c for c, _ in role.grants} for role in reg.DEFAULT_ROLES)


def test_hold_lifecycle_permission_step_up_and_maker_checker(app, docs_world):
    """EC-SEC-16: placing and releasing a hold: permission, step-up and maker-checker."""
    w = docs_world
    ids = scenario(w)
    a, b = officers(app, w["acme"])
    founder, emma = w["acme"], w["logins"]["Emma"]
    # No permission: 403 on every endpoint. The founder (company administrator) reads through payroll:audit:read,
    # which administrators hold implicitly, but never acts: the manage permission is not implied.
    assert founder.get(HOLDS).status_code == 200 and founder.get(f"{HOLDS}/summary").status_code == 200
    for client in (emma, w["clerk"]):
        assert client.get(HOLDS).status_code == 403
        assert client.get(f"{HOLDS}/summary").status_code == 403
        assert client.get(f"{HOLDS}/sweeps").status_code == 403
    for client in (founder, emma, w["clerk"]):
        assert client.post(HOLDS, json={"scope": "DOCUMENT", "document_id": ids["emma"], "reason": "x"}
                           ).status_code == 403
        assert client.post(f"{HOLDS}/sweeps").status_code == 403
    # An auditor (payroll:audit:read) may look, never act.
    auditor_role = role(founder, "Payroll auditors", "COMPANY", ["payroll:audit:read"])
    auditor = user_with_role(app, founder, create_employee(founder, "Audie", hire_date="2024-01-15"), auditor_role)
    assert auditor.get(HOLDS).status_code == 200 and auditor.get(f"{HOLDS}/summary").status_code == 200
    assert auditor.post(HOLDS, json={"scope": "DOCUMENT", "document_id": ids["emma"], "reason": "x"}
                        ).status_code == 403
    # Validation: exactly the target of the scope, strict body, known document types, foreign ids are 404.
    def post(**body):
        return a.post(HOLDS, json={"reason": "Litigation hold", **body})

    assert post(scope="EMPLOYEE").json()["code"] == "INVALID_TARGET"
    assert post(scope="EMPLOYEE", employee_id=w["emp"]["Emma"]["id"], document_id=ids["emma"]).status_code == 400
    assert post(scope="DOCUMENT", document_id=ids["emma"], company_id=2).status_code == 422
    assert post(scope="DOCUMENT_TYPE", doc_type="PAYCHECK").status_code == 422
    assert post(scope="PAYSLIP", doc_type="PAYSLIP_V2").status_code == 422
    assert a.post(HOLDS, json={"scope": "DOCUMENT", "document_id": ids["emma"], "reason": "  "}).status_code == 422
    assert post(scope="EMPLOYEE", employee_id=999999).status_code == 404
    assert post(scope="LEGAL_ENTITY", legal_entity_id=999999).status_code == 404
    assert post(scope="DOCUMENT", document_id=999999).status_code == 404
    # Creating needs no step-up; an Idempotency-Key makes a retry return the same hold.
    body = {"scope": "DOCUMENT", "document_id": ids["emma"], "reason": "Tribunal claim", "reference": "TRB-7"}
    first = a.post(HOLDS, json=body, headers={"Idempotency-Key": "hold-1"})
    again = a.post(HOLDS, json=body, headers={"Idempotency-Key": "hold-1"})
    assert first.status_code == again.status_code == 201 and first.json()["id"] == again.json()["id"]
    hold = first.json()
    assert hold["created_by_user_id"] == a.user_id and hold["reference"] == "TRB-7" and hold["row_version"] == 1
    assert sql("SELECT count(*) FROM legal_holds WHERE company_id = :c", c=w["cid"])[0][0] == 1
    release = f"{HOLDS}/{hold['id']}/release"
    # Maker-checker: the creator cannot release, with or without a step-up.
    step_up(a)
    r = a.post(release, json={"reason": "Done"})
    assert r.status_code == 403 and r.json()["code"] == "MAKER_CHECKER_REQUIRED"
    # Another officer needs a recent step-up (none, and a stale one, are refused).
    r = b.post(release, json={"reason": "Done"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    step_up(b, minutes_ago=10)
    assert b.post(release, json={"reason": "Done"}).json()["code"] == "STEP_UP_REQUIRED"
    step_up(b)
    assert b.post(release, json={"reason": " "}).status_code == 422
    assert b.post(release, json={"reason": "Done", "row_version": 5}).status_code == 409
    assert b.post(release, json={"reason": "Done", "extra": 1}).status_code == 422
    assert w["acme"].post(release, json={"reason": "Done"}).status_code == 403
    done = b.post(release, json={"reason": "Tribunal claim withdrawn"})
    assert done.status_code == 200, done.text
    released = done.json()
    assert released["status"] == "RELEASED" and released["released_by_user_id"] == b.user_id
    assert released["release_reason"] == "Tribunal claim withdrawn" and released["row_version"] == 2
    assert b.post(release, json={"reason": "Again"}).json()["code"] == "LEGAL_HOLD_RELEASED"
    assert a.post(f"{HOLDS}/999999/release", json={"reason": "x"}).status_code in (403, 404)
    step_up(a)
    assert a.post(f"{HOLDS}/999999/release", json={"reason": "x"}).status_code == 404
    # Lists, filters and the read of one hold.
    assert [h["status"] for h in a.get(HOLDS).json()["items"]] == ["RELEASED"]
    assert a.get(HOLDS, params={"status": "ACTIVE"}).json()["total"] == 0
    assert a.get(HOLDS, params={"scope": "DOCUMENT"}).json()["total"] == 1
    assert a.get(HOLDS, params={"status": "FROZEN"}).status_code == 422
    assert a.get(f"{HOLDS}/{hold['id']}").json()["id"] == hold["id"]
    assert audit_actions(w["cid"], "legal_hold", hold["id"]) == ["LEGAL_HOLD_CREATED", "LEGAL_HOLD_RELEASED"]
    # The manual sweep needs the step-up as well.
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=a.user_id)
    assert a.post(f"{HOLDS}/sweeps").json()["code"] == "STEP_UP_REQUIRED"


def test_hold_rows_are_guarded_in_the_database(app, docs_world):
    w = docs_world
    ids = scenario(w)
    a, b = officers(app, w["acme"])
    hold = place(a, scope="DOCUMENT", document_id=ids["emma"])
    step_up(b)
    assert b.post(f"{HOLDS}/{hold['id']}/release", json={"reason": "Done"}).status_code == 200
    live = place(a, scope="DOCUMENT", document_id=ids["ivan"])
    cid = w["cid"]
    _refused(cid, "UPDATE legal_holds SET reason = 'edited' WHERE id = :i", "LEGAL_HOLD_IMMUTABLE", i=live["id"])
    _refused(cid, "UPDATE legal_holds SET document_id = :d WHERE id = :i", "LEGAL_HOLD_IMMUTABLE",
             d=ids["ivan_oct"], i=live["id"])
    _refused(cid, "DELETE FROM legal_holds WHERE id = :i", "LEGAL_HOLD_IMMUTABLE", i=live["id"])
    _refused(cid, "UPDATE legal_holds SET status = 'ACTIVE', released_at = NULL, released_by_user_id = NULL, "
                  "release_reason = NULL WHERE id = :i", "LEGAL_HOLD_IMMUTABLE", i=hold["id"])  # never reopens
    with tenant(cid) as conn:  # maker-checker and the target/status rules also hold as plain constraints
        expect_error(conn, CHECK, "UPDATE legal_holds SET status = 'RELEASED', released_at = now(), "
                                  "released_by_user_id = :u, release_reason = 'x' WHERE id = :i",
                     u=a.user_id, i=live["id"])
        expect_error(conn, CHECK, "UPDATE legal_holds SET status = 'RELEASED' WHERE id = :i", i=live["id"])
        expect_error(conn, CHECK, "INSERT INTO legal_holds (company_id, scope, reason, status, created_by_user_id, "
                                  "row_version, created_at, updated_at) VALUES (:c, 'EMPLOYEE', 'r', 'ACTIVE', :u, "
                                  "1, now(), now())", c=cid, u=a.user_id)
        expect_error(conn, CHECK, "INSERT INTO legal_holds (company_id, scope, employee_id, document_id, reason, "
                                  "status, created_by_user_id, row_version, created_at, updated_at) VALUES (:c, "
                                  "'EMPLOYEE', :e, :d, 'r', 'ACTIVE', :u, 1, now(), now())",
                     c=cid, e=w["emp"]["Emma"]["id"], d=ids["emma"], u=a.user_id)


# ---------------------------------------------------------------------------
# Three tenants
# ---------------------------------------------------------------------------
def test_holds_and_sweeps_are_isolated_across_three_tenants(app, docs_world):
    """EC-SEC-16: holds and sweeps never cross tenants (three tenants)."""
    from app.models import TENANT_TABLES

    assert "legal_holds" in TENANT_TABLES
    w = docs_world
    ids = scenario(w)
    beta, cobalt = w["beta"], w["cobalt"]
    for t in (beta, cobalt):
        run_id, rows = finalized_run(t["cid"], t["entity"], t["group"], date(2026, 9, 1), [t["employee"]["id"]])
        generate(t["cid"], run_id)
        t["doc"] = payslip_of(rows[t["employee"]["id"]])[0]
        t["path"] = storage_path(t["doc"])
        age(t["doc"])
    a, _ = officers(app, w["acme"])
    (bo,) = officers(app, beta["client"], prefix="Bea", count=1)
    (co,) = officers(app, cobalt["client"], prefix="Cora", count=1)
    held_entity = place(a, scope="LEGAL_ENTITY", legal_entity_id=w["le01"])  # holds everything of Acme's LE01
    # Acme's hold never reaches the other tenants; their own sweeps run freely.
    for client, t in ((bo, beta), (co, cobalt)):
        step_up(client)
        assert client.get(HOLDS).json()["total"] == 0
        assert client.get(f"{HOLDS}/summary").json()["active_holds"] == 0
        counts = sweep_counts(client)
        assert (counts["due"], counts["held"], counts["redacted"]) == (1, 0, 1)
        assert redacted(t["doc"]) and not file_exists(t["cid"], t["path"])
    step_up(a)
    counts = sweep_counts(a)
    assert (counts["due"], counts["held"], counts["redacted"]) == (3, 3, 0)  # only Acme's own documents counted
    # Foreign ids are 404 everywhere: holds, employees, documents; releasing a foreign hold changes nothing.
    assert bo.get(f"{HOLDS}/{held_entity['id']}").status_code == 404
    step_up(bo)
    assert bo.post(f"{HOLDS}/{held_entity['id']}/release", json={"reason": "x"}).status_code == 404
    assert a.post(HOLDS, json={"scope": "EMPLOYEE", "employee_id": beta["employee"]["id"], "reason": "x"}
                  ).status_code == 404
    assert a.post(HOLDS, json={"scope": "DOCUMENT", "document_id": beta["doc"], "reason": "x"}).status_code == 404
    assert a.post(HOLDS, json={"scope": "LEGAL_ENTITY", "legal_entity_id": beta["entity"], "reason": "x"}
                  ).status_code == 404
    assert erase(a, beta["employee"]["id"]).status_code == 404
    assert sql("SELECT status FROM legal_holds WHERE id = :i", i=held_entity["id"])[0][0] == "ACTIVE"
    assert sql("SELECT count(*) FROM legal_holds WHERE company_id <> :c", c=w["cid"])[0][0] == 0
    # Row-level security and composite keys.
    cids = (w["cid"], beta["cid"], cobalt["cid"])
    with tenant(None) as conn:
        assert conn.execute(text("SELECT count(*) FROM legal_holds")).scalar() == 0
    for cid in cids:
        with tenant(cid) as conn:
            assert {o for (o,) in conn.execute(text("SELECT company_id FROM legal_holds"))} <= {cid}
            assert conn.execute(text("SELECT count(*) FROM legal_holds WHERE id = :i"),
                                {"i": held_entity["id"]}).scalar() == (1 if cid == w["cid"] else 0)
            assert conn.execute(text("UPDATE legal_holds SET updated_at = now() WHERE id = :i"),
                                {"i": held_entity["id"]}).rowcount == (1 if cid == w["cid"] else 0)
    with tenant(w["cid"]) as conn:
        for column, value in (("employee_id", beta["employee"]["id"]), ("legal_entity_id", beta["entity"]),
                              ("document_id", beta["doc"])):
            scope = {"employee_id": "EMPLOYEE", "legal_entity_id": "LEGAL_ENTITY", "document_id": "DOCUMENT"}[column]
            expect_error(conn, FOREIGN_KEY,
                         f"INSERT INTO legal_holds (company_id, scope, {column}, reason, status, created_by_user_id, "
                         "row_version, created_at, updated_at) VALUES (:c, :s, :v, 'r', 'ACTIVE', :u, 1, now(), now())",
                         c=w["cid"], s=scope, v=value, u=a.user_id)
        expect_error(conn, CHECK, "UPDATE legal_holds SET company_id = :o WHERE id = :i", o=beta["cid"],
                     i=held_entity["id"])
    # Beta's documents are unaffected by anything Acme does and the reverse.
    assert not redacted(ids["emma"]) and file_exists(w["cid"], ids["paths"]["emma"])


# ---------------------------------------------------------------------------
# The periodic job
# ---------------------------------------------------------------------------
def test_periodic_job_sweeps_audits_and_requeues_itself(docs_world):
    """EC-SEC-16: the periodic retention job sweeps, audits counts only and re-queues itself."""
    from app.payroll.services import jobs, legal_holds

    w = docs_world
    cid = w["cid"]
    ids = scenario(w)
    with session(cid) as (db, _):
        job = jobs.enqueue(db, company_id=cid, type=legal_holds.SWEEP_JOB, payload={"slot": "test"},
                           idempotency_key="retention-sweep:test")
        job_id = job.id
        db.commit()  # JOBS_MODE=inline: the job runs right after this commit
    status, result = sql("SELECT status, result FROM jobs WHERE id = :i", i=job_id)[0]
    assert status == "SUCCEEDED" and result["due"] == 3 and result["redacted"] == 3 and result["held"] == 0
    assert all(redacted(ids[k]) for k in ("emma", "ivan", "register")) and not redacted(ids["emma_oct"])
    [details] = sweep_audit_details(cid)
    assert details["trigger"] == "JOB" and set(details) == COUNT_KEYS and details["redacted"] == 3
    # It queued the next sweep (02:30 UTC) and nothing else.
    queued = sql("SELECT run_at, idempotency_key FROM jobs WHERE company_id = :c AND type = 'payroll.retention_sweep' "
                 "AND status = 'QUEUED'", c=cid)
    assert len(queued) == 1
    run_at, key = queued[0]
    assert (run_at.hour, run_at.minute) == (2, 30) and run_at > legal_holds.clock.utcnow()
    assert key == f"retention-sweep:{run_at:%Y%m%dT%H%M}"


def test_daily_maintenance_keeps_one_sweep_queued_per_day(docs_world):
    from app.payroll.services import calendars

    cid = docs_world["cid"]
    for _ in range(2):  # idempotent: scheduling twice for the same day queues one job
        with session(cid) as (db, _):
            calendars.schedule_generation(db, cid)
            db.commit()
    rows = sql("SELECT run_at, idempotency_key, payload FROM jobs WHERE company_id = :c AND "
               "type = 'payroll.retention_sweep'", c=cid)
    assert len(rows) == 1 and (rows[0][0].hour, rows[0][0].minute) == (2, 30)
    assert rows[0][1].startswith("retention-sweep:") and "slot" in rows[0][2]


def test_next_slot_is_the_next_0230_utc():
    from datetime import datetime, timezone

    from app.payroll.services.legal_holds import next_slot

    utc = timezone.utc
    assert next_slot(datetime(2026, 10, 24, 1, 0, tzinfo=utc)) == datetime(2026, 10, 24, 2, 30, tzinfo=utc)
    assert next_slot(datetime(2026, 10, 24, 2, 30, tzinfo=utc)) == datetime(2026, 10, 25, 2, 30, tzinfo=utc)
    assert next_slot(datetime(2026, 12, 31, 23, 59, tzinfo=utc)) == datetime(2027, 1, 1, 2, 30, tzinfo=utc)
    assert timedelta(minutes=10) == __import__("app.payroll.services.legal_holds", fromlist=["x"]).CONTINUE_AFTER
