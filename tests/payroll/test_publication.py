"""Payslip generation, publication triggers, notifications, corrections and reversal (3.6 §5-§7.1).

EC-DOC-02 (unpublished payslips are invisible to the employee), EC-DOC-04 (bulk generation is idempotent and
retried per employee), EC-DOC-05 (one notification per payslip version), EC-DOC-11 (ON_SETTLED with a failed
payment stays unpublished; HR may publish manually with a reason), EC-DOC-12 (a reversed run's payslips become
REVERSED and a reversal statement is issued), EC-DOC-14 (attachments only with PDF protection, V-09),
EC-DOC-15 (the PDF password is set by the employee after step-up and never shown), EC-DOC-16 (storage outage:
retries, then FAILED; no partial files).
"""

import email
import shutil
from datetime import date, datetime, timedelta, timezone
from email import policy
from pathlib import Path
from types import SimpleNamespace

from tests.payroll.test_payslips_v2 import (  # noqa: F401  (docs_world is a fixture)
    ME,
    PAY,
    add_instruction,
    content_of,
    docs_world,
    emma_run,
    finalized_run,
    generate,
    payslip_of,
    pdf_text,
)
from tests.payroll.test_run_finalize import fin_world  # noqa: F401  (fixture)
from tests.payroll.test_runs_lifecycle import runs_world  # noqa: F401  (fixture used by fin_world)
from tests.payroll.test_wage_inputs import audit_actions, session, set_setting, sql


def outbox_dir(cid):
    from app.config import settings

    return Path(settings.email_outbox_dir).resolve() / str(cid)


def clear_outbox(cid):
    shutil.rmtree(outbox_dir(cid), ignore_errors=True)


def payslip_emails(cid):
    files = sorted(outbox_dir(cid).glob("payroll.payslip_published-*.eml"))
    return [email.message_from_bytes(f.read_bytes(), policy=policy.default) for f in files]


def body_text(message):
    return message.get_body(preferencelist=("plain",)).get_content()


def doc_status(document_id):
    return sql("SELECT status, publish_trigger, publish_reason FROM documents WHERE id = :d", d=document_id)[0]


# ---------------------------------------------------------------------------
# Generation (EC-DOC-04, EC-DOC-16)
# ---------------------------------------------------------------------------
def test_ec_doc_04_generation_is_idempotent_per_run_employee_and_version(docs_world):
    from app.payroll.services import outbox

    w = docs_world
    run_id, rows = emma_run(w)
    first = generate(w["cid"], run_id)
    assert first["generated"] == 2 and first["failed"] == 0 and first["published"] == 2
    again = generate(w["cid"], run_id)
    assert again["queued"] == 0 and again["generated"] == 0
    with session(w["cid"]) as (db, _):  # the real trigger: the outbox event of the finalized run, twice
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        db.commit()
    assert sql("SELECT count(*) FROM documents WHERE run_id = :r AND doc_type = 'PAYSLIP_V2'", r=run_id)[0][0] == 2
    tasks = sql("SELECT status, document_version FROM document_generation_tasks WHERE run_id = :r", r=run_id)
    assert sorted(tasks) == [("GENERATED", 1), ("GENERATED", 1)]


def test_outbox_event_generates_and_publishes_payslips(docs_world):
    from app.payroll.services import outbox

    w = docs_world
    run_id, rows = emma_run(w)
    with session(w["cid"]) as (db, _):
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        db.commit()
    for row in rows.values():
        assert payslip_of(row)[1] == "PUBLISHED"
    job = sql("SELECT status FROM jobs WHERE type = 'documents.run_finalized'")
    assert job == [("SUCCEEDED",)]
    # payslip.email_notify OFF (docs_world): in-app only, no e-mail row and no e-mail job.
    channels = sql("SELECT DISTINCT channel FROM document_notifications WHERE company_id = :c", c=w["cid"])
    assert channels == [("IN_APP",)]
    assert sql("SELECT count(*) FROM jobs WHERE type = 'documents.payslip_email'")[0][0] == 0


def test_ec_doc_16_storage_outage_retries_then_fails_and_never_leaves_partial_files(docs_world, monkeypatch):
    from app.core import storage
    from app.models.pay_runs import PayrollRun
    from app.payroll.services import documents, publication

    w = docs_world
    run_id, rows = emma_run(w)
    emma_row = rows[w["emp"]["Emma"]["id"]]
    real_log = documents.AuditLogService.log
    failing = {"on": True}

    def flaky(db, principal, module, action, *args, **kwargs):
        # The file is already written when the row's audit fails: the savepoint and the file both go.
        if failing["on"] and action == "DOCUMENT_GENERATED" and kwargs.get("details", {}).get(
                "source_key") == f"run_employee:{emma_row}":
            raise OSError("object storage unavailable")
        return real_log(db, principal, module, action, *args, **kwargs)

    monkeypatch.setattr(documents.AuditLogService, "log", staticmethod(flaky))
    folder = Path(storage._root()) / str(w["cid"]) / documents.STORAGE_CATEGORY
    before = set(folder.glob("*.pdf"))
    result = generate(w["cid"], run_id)
    assert (result["generated"], result["retrying"], result["failed"]) == (1, 1, 0)
    assert payslip_of(emma_row) is None
    task = sql("SELECT status, attempts, last_error FROM document_generation_tasks WHERE run_employee_id = :r",
               r=emma_row)[0]
    assert task[0] == "QUEUED" and task[1] == 1 and "object storage unavailable" in task[2]
    retry_jobs = sql("SELECT run_at > now() FROM jobs WHERE type = 'documents.generate_payslips'")
    assert retry_jobs == [(True,)]  # retried with backoff, never at once
    for _ in range(2):
        with session(w["cid"]) as (db, _):
            publication.process_tasks(db, db.get(PayrollRun, run_id))
            db.commit()
    task = sql("SELECT status, attempts FROM document_generation_tasks WHERE run_employee_id = :r", r=emma_row)[0]
    assert task == ("FAILED", 3)
    # Only the generated payslip left a file: the failed attempts' files were removed with their savepoints.
    assert len(set(folder.glob("*.pdf")) - before) == sql("SELECT count(*) FROM documents WHERE company_id = :c",
                                                           c=w["cid"])[0][0] == 1
    status = w["acme"].get(f"{PAY}/runs/{run_id}/payslips/generation").json()
    assert status["counts"] == {"QUEUED": 0, "GENERATED": 1, "FAILED": 1}
    assert status["failed"][0]["error"].startswith("OSError")
    failing["on"] = False
    r = w["acme"].post(f"{PAY}/runs/{run_id}/payslips/retry-failed")
    assert r.status_code == 202 and r.json()["requeued"] == 1
    assert payslip_of(emma_row)[1] == "PUBLISHED"
    assert "PAYSLIP_GENERATION_FAILED" in audit_actions(w["cid"], "payroll_run", run_id)


def test_generation_endpoints_need_permission_and_scope(docs_world):
    w = docs_world
    run_id, _ = emma_run(w)
    generate(w["cid"], run_id)
    emma = w["logins"]["Emma"]
    assert emma.get(f"{PAY}/runs/{run_id}/payslips/generation").status_code == 403
    assert emma.post(f"{PAY}/runs/{run_id}/payslips/retry-failed").status_code == 403
    assert w["clerk"].get(f"{PAY}/runs/{run_id}/payslips/generation").status_code == 404  # other entity
    assert w["beta"]["client"].get(f"{PAY}/runs/{run_id}/payslips/generation").status_code in (403, 404)
    r = w["acme"].post(f"{PAY}/runs/{run_id}/payslips/generate")
    assert r.status_code == 202 and r.json()["queued"] == 0


# ---------------------------------------------------------------------------
# Publication triggers (EC-DOC-02, EC-DOC-11)
# ---------------------------------------------------------------------------
def test_ec_doc_02_and_ec_doc_11_on_settled_failed_payment_stays_unpublished_until_manual_publish(docs_world):
    from app.payroll.services import publication

    w = docs_world
    set_setting(w["cid"], "payslip.publish_trigger", "ON_SETTLED")
    run_id, rows = emma_run(w)
    emma_row = rows[w["emp"]["Emma"]["id"]]
    add_instruction(w["cid"], run_id, emma_row, w["emp"]["Emma"]["id"], w["le01"], w["group"], status="FAILED",
                    channel="API")
    generate(w["cid"], run_id)
    doc_id = payslip_of(emma_row)[0]
    assert doc_status(doc_id)[0] == "GENERATED"
    with session(w["cid"]) as (db, _):
        assert publication.on_payment_progress(db, w["cid"], run_id)["published"] == 0
        db.commit()
    emma = w["logins"]["Emma"]
    assert emma.get(f"{ME}/payslips").json()["total"] == 0  # EC-DOC-02
    assert emma.get(f"{ME}/payslips/{doc_id}").status_code == 404
    assert emma.get(f"{PAY}/payslips/{doc_id}").status_code == 404
    assert emma.get(f"{ME}/payslips/{doc_id}/download").status_code == 404
    # Manual publication needs the permission, the scope and a reason (EC-DOC-11).
    assert emma.post(f"{PAY}/payslips/{doc_id}/publish", json={"reason": "x"}).status_code == 403
    assert w["clerk"].post(f"{PAY}/payslips/{doc_id}/publish", json={"reason": "x"}).status_code == 404
    assert w["acme"].post(f"{PAY}/payslips/{doc_id}/publish", json={}).status_code == 422
    assert w["acme"].post(f"{PAY}/payslips/{doc_id}/publish", json={"reason": "   "}).status_code == 422
    assert w["acme"].post(f"{PAY}/payslips/{doc_id}/publish",
                          json={"reason": "x", "status": "PUBLISHED"}).status_code == 422  # strict body
    r = w["acme"].post(f"{PAY}/payslips/{doc_id}/publish", json={"reason": "Paid by cheque after the bank failure"})
    assert r.status_code == 200, r.text
    assert doc_status(doc_id) == ("PUBLISHED", "MANUAL", "Paid by cheque after the bank failure")
    assert "PAYSLIP_MANUAL_PUBLISH" in audit_actions(w["cid"], "document", doc_id)
    assert w["acme"].post(f"{PAY}/payslips/{doc_id}/publish", json={"reason": "again"}).status_code == 409
    listed = emma.get(f"{ME}/payslips").json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == doc_id


def test_on_payment_submitted_and_scheduled_triggers(docs_world):
    from app.payroll.services import publication

    w = docs_world
    set_setting(w["cid"], "payslip.publish_trigger", "ON_PAYMENT_SUBMITTED")
    run_id, rows = emma_run(w)
    emma_row, ivan_row = rows[w["emp"]["Emma"]["id"]], rows[w["emp"]["Ivan"]["id"]]
    generate(w["cid"], run_id)
    assert payslip_of(emma_row)[1] == payslip_of(ivan_row)[1] == "GENERATED"
    add_instruction(w["cid"], run_id, emma_row, w["emp"]["Emma"]["id"], w["le01"], w["group"], status="SUBMITTED")
    with session(w["cid"]) as (db, _):
        assert publication.on_payment_progress(db, w["cid"], run_id)["published"] == 1
        db.commit()
    assert payslip_of(emma_row)[1] == "PUBLISHED" and payslip_of(ivan_row)[1] == "GENERATED"
    assert doc_status(payslip_of(emma_row)[0])[1] == "ON_PAYMENT_SUBMITTED"
    sweeps = sql("SELECT count(*) FROM jobs WHERE type = 'documents.publication_sweep'")[0][0]
    assert sweeps >= 1  # the rest is re-checked later

    set_setting(w["cid"], "payslip.publish_trigger", "SCHEDULED")
    set_setting(w["cid"], "payslip.scheduled_publish_time", "10:00")
    run_id, rows = emma_run(w, start=date(2026, 10, 1))
    generate(w["cid"], run_id)
    row = rows[w["emp"]["Emma"]["id"]]
    assert payslip_of(row)[1] == "GENERATED"
    pay_moment = datetime(2026, 10, 31, 4, 30, tzinfo=timezone.utc)  # 10:00 in Asia/Kolkata
    with session(w["cid"]) as (db, _):
        assert publication.apply_publication(db, w["cid"], run_id, now=pay_moment - timedelta(minutes=1))[
            "published"] == 0
        assert publication.apply_publication(db, w["cid"], run_id, now=pay_moment)["published"] == 2
        db.commit()
    assert payslip_of(row)[1] == "PUBLISHED"


def test_trigger_rules_are_evidence_based():
    from app.payroll.services.publication import trigger_met

    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    paid = SimpleNamespace(status="SETTLED", evidence_status="VERIFIED", submitted_at=now)
    unverified = SimpleNamespace(status="SUBMITTED", evidence_status="UNVERIFIED", submitted_at=now)
    failed = SimpleNamespace(status="FAILED", evidence_status="UNVERIFIED", submitted_at=now)
    pending = SimpleNamespace(status="READY", evidence_status="UNVERIFIED", submitted_at=None)
    common = {"scheduled_at": None, "now": now}
    from decimal import Decimal

    assert trigger_met("ON_FINALIZE", net=Decimal(1), instructions=[], **common)
    assert trigger_met("ON_SETTLED", net=Decimal(1), instructions=[paid], **common)
    assert not trigger_met("ON_SETTLED", net=Decimal(1), instructions=[paid, unverified], **common)
    assert not trigger_met("ON_SETTLED", net=Decimal(1), instructions=[failed], **common)  # EC-DOC-11
    assert not trigger_met("ON_SETTLED", net=Decimal(1), instructions=[], **common)
    assert trigger_met("ON_SETTLED", net=Decimal(0), instructions=[], **common)  # nothing to pay
    assert trigger_met("ON_PAYMENT_SUBMITTED", net=Decimal(1), instructions=[unverified], **common)
    assert not trigger_met("ON_PAYMENT_SUBMITTED", net=Decimal(1), instructions=[pending], **common)
    assert not trigger_met("SCHEDULED", net=Decimal(1), instructions=[], scheduled_at=now + timedelta(seconds=1),
                           now=now)


def test_publish_run_endpoint_applies_trigger_or_publishes_manually_with_reason(docs_world):
    w = docs_world
    set_setting(w["cid"], "payslip.publish_trigger", "ON_SETTLED")
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    r = w["acme"].post(f"{PAY}/runs/{run_id}/publish", json={})
    assert r.status_code == 200 and r.json()["published"] == 0 and r.json()["published_manually"] == 0
    assert w["logins"]["Emma"].post(f"{PAY}/runs/{run_id}/publish", json={}).status_code == 403
    r = w["acme"].post(f"{PAY}/runs/{run_id}/publish", json={"reason": "Payments confirmed offline"})
    assert r.status_code == 200 and r.json()["published_manually"] == 2
    assert {payslip_of(row)[1] for row in rows.values()} == {"PUBLISHED"}


# ---------------------------------------------------------------------------
# Notifications (EC-DOC-05, EC-DOC-14, EC-DOC-15)
# ---------------------------------------------------------------------------
def test_ec_doc_05_one_notification_per_payslip_version(docs_world):
    from app.models.pay_documents import PayDocument
    from app.payroll.services import publication

    w = docs_world
    set_setting(w["cid"], "payslip.email_notify", True)  # the starter default (STARTER_V1)
    clear_outbox(w["cid"])
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_row = rows[w["emp"]["Emma"]["id"]]
    doc_id = payslip_of(emma_row)[0]
    emma_user = w["logins"]["Emma"].user_id
    notes = sql("SELECT title, action_url FROM notifications WHERE user_id = :u", u=emma_user)
    assert notes == [("Your payslip for September 2026 is ready", f"/me/payslips/{doc_id}")]
    assert sql("SELECT channel, status FROM document_notifications WHERE document_id = :d ORDER BY channel",
               d=doc_id) == [("EMAIL", "SENT"), ("IN_APP", "SENT")]
    assert doc_status(doc_id)[0] == "EMAIL_SENT"
    assert sql("SELECT action, user_id FROM document_access_log WHERE document_id = :d", d=doc_id) == [("EMAIL", None)]
    emails = [m for m in payslip_emails(w["cid"]) if m["To"] == w["emp"]["Emma"]["email"]]
    assert len(emails) == 1
    text_ = body_text(emails[0])
    assert "68,700" not in text_ and "68700" not in text_ and "does not include amounts" in text_
    assert not list(emails[0].iter_attachments())
    # Clicked twice / retried: nothing new.
    with session(w["cid"]) as (db, _):
        doc = db.get(PayDocument, doc_id)
        assert publication.notify(db, doc) == {}
        assert publication.send_email(db, w["cid"], doc_id) == {"skipped": True}
        db.commit()
    r = w["acme"].post(f"{PAY}/payslips/{doc_id}/resend")
    assert r.status_code == 409 and r.json()["code"] == "ALREADY_NOTIFIED"
    assert len([m for m in payslip_emails(w["cid"]) if m["To"] == w["emp"]["Emma"]["email"]]) == 1
    assert sql("SELECT count(*) FROM notifications WHERE user_id = :u", u=emma_user)[0][0] == 1
    # A failed delivery may be retried; still one notification row per channel.
    sql("UPDATE document_notifications SET status = 'FAILED' WHERE document_id = :d AND channel = 'EMAIL'", d=doc_id)
    r = w["acme"].post(f"{PAY}/payslips/{doc_id}/resend")
    assert r.status_code == 200, r.text
    assert sql("SELECT count(*), max(status) FROM document_notifications WHERE document_id = :d AND channel = 'EMAIL'",
               d=doc_id)[0] == (1, "SENT")
    assert w["logins"]["Emma"].post(f"{PAY}/payslips/{doc_id}/resend").status_code == 403


def test_email_with_net_pay_and_ec_doc_14_attachment_only_with_pdf_protection(docs_world):
    import pymupdf

    from app.payroll import settings_registry as reg
    from app.payroll.services import publication

    w = docs_world
    issues = reg.cross_validate({"payslip.email_attach_pdf": True, "payslip.pdf_protection": "NONE",
                                 "payslip.email_notify": True})
    assert any(i.rule == "V-09" and i.severity == "ERROR" for i in issues)  # blocked by validation (EC-DOC-14)
    clear_outbox(w["cid"])
    set_setting(w["cid"], "payslip.email_notify", True)
    set_setting(w["cid"], "payslip.email_include_net_pay", True)
    set_setting(w["cid"], "payslip.email_attach_pdf", True)  # forced past validation: still never unprotected
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    doc_id = payslip_of(rows[w["emp"]["Emma"]["id"]])[0]
    message = next(m for m in payslip_emails(w["cid"]) if m["To"] == w["emp"]["Emma"]["email"])
    assert "Net pay: ₹68,700.00." in body_text(message)
    assert not list(message.iter_attachments())
    assert "Attachment blocked" in sql("SELECT detail FROM document_notifications WHERE document_id = :d AND "
                                       "channel = 'EMAIL'", d=doc_id)[0][0]
    # With EMPLOYEE_SECRET and the employee's own password the attachment is encrypted (AES-256).
    set_setting(w["cid"], "payslip.pdf_protection", "EMPLOYEE_SECRET")
    with session(w["cid"], w["logins"]["Emma"].user_id) as (db, principal):
        publication.set_employee_secret(db, principal, "correct-horse-42")
        db.commit()
    clear_outbox(w["cid"])
    run_id, rows = emma_run(w, start=date(2026, 10, 1))
    generate(w["cid"], run_id)
    message = next(m for m in payslip_emails(w["cid"]) if m["To"] == w["emp"]["Emma"]["email"])
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1 and attachments[0].get_content_type() == "application/pdf"
    with pymupdf.open(stream=attachments[0].get_content(), filetype="pdf") as pdf:
        assert pdf.needs_pass
        assert pdf.authenticate("wrong-password") == 0
        assert pdf.authenticate("correct-horse-42") > 0
    secret = sql("SELECT secret_enc FROM payslip_pdf_secrets")[0][0]
    assert "correct-horse-42" not in secret


def test_ec_doc_15_pdf_password_is_set_after_step_up_and_never_shown(docs_world):
    from tests.payroll.test_step_up import _clear_outbox, _emailed_code

    w = docs_world
    emma = w["logins"]["Emma"]
    assert emma.get(f"{ME}/payslip-password").json() == {"set": False, "set_at": None}
    r = emma.put(f"{ME}/payslip-password", json={"password": "correct-horse-42"})
    assert r.status_code == 403 and r.json()["code"] == "STEP_UP_REQUIRED"
    _clear_outbox(w["cid"])
    assert emma.post("/api/v1/payroll/step-up/email-code").status_code == 200
    code, _ = _emailed_code(w["cid"], emma.email)
    assert emma.post("/api/v1/payroll/step-up", json={"code": code}).status_code == 200
    assert emma.put(f"{ME}/payslip-password", json={"password": "short"}).status_code == 422
    assert emma.put(f"{ME}/payslip-password", json={"password": "correct-horse-42", "employee_id": 1}
                    ).status_code == 422
    sql("UPDATE employees SET date_of_birth = '1990-01-15' WHERE id = :e", e=w["emp"]["Emma"]["id"])
    r = emma.put(f"{ME}/payslip-password", json={"password": "15011990"})  # never a guessable date of birth
    assert r.status_code == 400 and r.json()["code"] == "PDF_PASSWORD_GUESSABLE"
    r = emma.put(f"{ME}/payslip-password", json={"password": "correct-horse-42"})
    assert r.status_code == 200 and r.json()["set"] is True and "correct-horse" not in r.text
    status = emma.get(f"{ME}/payslip-password").json()
    assert status["set"] is True and "correct-horse" not in str(status)
    assert "PAYSLIP_PASSWORD_SET" in audit_actions(w["cid"], "employee", w["emp"]["Emma"]["id"])
    details = sql("SELECT details::text FROM audit_logs WHERE action LIKE 'PAYSLIP_PASSWORD%'")
    assert all("correct-horse" not in d for (d,) in details)


# ---------------------------------------------------------------------------
# Corrections and reversal (3.6 §6, EC-DOC-12)
# ---------------------------------------------------------------------------
def test_revised_payslip_supersedes_the_original(docs_world):
    from app.models.pay_documents import PayDocument
    from app.models.pay_runs import PayrollRun, PayrollRunEmployee
    from app.payroll.services import publication

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_row = rows[w["emp"]["Emma"]["id"]]
    original_id, _, original_number, *_ = payslip_of(emma_row)
    with session(w["cid"]) as (db, _):
        revised = publication.issue_revised_payslip(
            db, None, db.get(PayDocument, original_id), run=db.get(PayrollRun, run_id),
            row=db.get(PayrollRunEmployee, emma_row), reason="Bonus correction")
        db.commit()
        revised_id = revised.id
    assert doc_status(original_id)[0] == "SUPERSEDED"
    revised_row = payslip_of(emma_row)
    assert revised_row[0] == revised_id and revised_row[3] == 2 and revised_row[1] == "PUBLISHED"
    assert revised_row[2] != original_number
    content = content_of(revised_id)
    assert content["title_key"] == "REVISED_PAYSLIP" and content["replaces"] == original_number
    listed = w["acme"].get(f"{PAY}/payslips", params={"run_id": run_id}).json()
    assert revised_id in [i["id"] for i in listed["items"]] and original_id not in [i["id"] for i in listed["items"]]
    everything = w["acme"].get(f"{PAY}/payslips", params={"run_id": run_id, "latest_only": False}).json()
    assert {original_id, revised_id} <= {i["id"] for i in everything["items"]}
    # The superseded original stays reachable for the employee.
    assert w["logins"]["Emma"].get(f"{ME}/payslips/{original_id}").json()["status"] == "SUPERSEDED"


def test_ec_doc_12_reversed_run_marks_payslips_reversed_and_issues_statements(docs_world):
    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_row = rows[w["emp"]["Emma"]["id"]]
    original_id = payslip_of(emma_row)[0]
    reversal_id, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 9, 1),
                                   [w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]], run_type="REVERSAL",
                                   reverses=run_id)
    assert generate(w["cid"], reversal_id) == {"reversed": 2}
    assert doc_status(original_id)[0] == "REVERSED"
    statement = sql("SELECT id, status, number, content FROM documents WHERE doc_type = 'REVERSAL_STATEMENT' AND "
                    "employee_id = :e", e=w["emp"]["Emma"]["id"])
    assert len(statement) == 1 and statement[0][1] == "PUBLISHED" and statement[0][2]
    assert statement[0][3]["tables"][0]["total_row"][-1] == "-68700.00"
    assert sql("SELECT reversal_document_id FROM documents WHERE id = :d", d=original_id)[0][0] == statement[0][0]
    assert generate(w["cid"], reversal_id) == {"reversed": 0}  # idempotent
    mine = w["logins"]["Emma"].get(f"{ME}/documents").json()
    assert statement[0][0] in [d["id"] for d in mine["items"]]
    text_ = pdf_text(w["logins"]["Emma"].get(
        w["logins"]["Emma"].get(f"{ME}/documents/{statement[0][0]}/download").json()["url"]).content)
    assert "Reversal statement" in text_
    # A reversed run never publishes again.
    from app.payroll.services import publication

    with session(w["cid"]) as (db, _):
        assert publication.apply_publication(db, w["cid"], run_id) == {"published": 0, "waiting": 0}


def test_ec_doc_12_payroll_run_reversed_event_reverses_only_the_reversed_employees(docs_world):
    """The corrections milestone emits ``payroll.run.reversed``; documents subscribe to it (outbox, exactly once)."""
    from app.payroll.services import outbox

    w = docs_world
    run_id, rows = emma_run(w)
    generate(w["cid"], run_id)
    emma_row, ivan_row = rows[w["emp"]["Emma"]["id"]], rows[w["emp"]["Ivan"]["id"]]
    emma_doc, ivan_doc = payslip_of(emma_row)[0], payslip_of(ivan_row)[0]
    # A selective reversal of Emma only (the REVERSAL run holds just her).
    reversal_id, _ = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 9, 1), [w["emp"]["Emma"]["id"]],
                                   run_type="REVERSAL", reverses=run_id)
    with session(w["cid"]) as (db, _):
        outbox.emit(db, "payroll.run.reversed", {"run_id": run_id, "reversal_run_id": reversal_id,
                                                 "reversal_id": 1, "run_employee_ids": [emma_row]})
        db.commit()
    assert doc_status(emma_doc)[0] == "REVERSED" and doc_status(ivan_doc)[0] == "PUBLISHED"
    statements = sql("SELECT employee_id, status FROM documents WHERE doc_type = 'REVERSAL_STATEMENT'")
    assert statements == [(w["emp"]["Emma"]["id"], "PUBLISHED")]
    assert sql("SELECT status FROM jobs WHERE type = 'documents.run_reversed'") == [("SUCCEEDED",)]
    # The same reversal arriving through payroll.run.finalized of the REVERSAL run issues nothing new.
    assert generate(w["cid"], reversal_id) == {"reversed": 0}
    assert sql("SELECT count(*) FROM documents WHERE doc_type = 'REVERSAL_STATEMENT'")[0][0] == 1
    # A forged payload naming another run is refused (never reverses unrelated payslips).
    from app.payroll.services import jobs, publication

    with session(w["cid"]) as (db, _):
        try:
            publication.handle_run_reversed(db, w["cid"], {"run_id": run_id + 999, "reversal_run_id": reversal_id})
            raise AssertionError("expected PermanentJobError")
        except jobs.PermanentJobError:
            pass
        db.rollback()
    assert doc_status(ivan_doc)[0] == "PUBLISHED"


def test_final_settlement_finalized_event_issues_the_full_and_final_statement(docs_world):
    """``payroll.final_settlement.finalized`` (corrections) -> published F&F statement for that employee only."""
    from app.payroll.services import outbox

    w = docs_world
    emma_id, ivan_id = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    settlement, rows = finalized_run(w["cid"], w["le01"], w["group"], date(2026, 10, 1), [emma_id, ivan_id],
                                     run_type="FINAL_SETTLEMENT")
    with session(w["cid"]) as (db, _):
        outbox.emit(db, "payroll.final_settlement.finalized", {"case_id": 1, "run_id": settlement,
                                                               "employee_id": emma_id})
        db.commit()
    found = sql("SELECT id, employee_id, status, number FROM documents WHERE doc_type = 'FINAL_SETTLEMENT_STATEMENT'")
    assert len(found) == 1 and found[0][1:3] == (emma_id, "PUBLISHED") and found[0][3]
    assert sql("SELECT status FROM jobs WHERE type = 'documents.final_settlement_statement'") == [("SUCCEEDED",)]
    assert w["logins"]["Emma"].get(f"{ME}/documents/{found[0][0]}").status_code == 200
    assert w["logins"]["Ivan"].get(f"{ME}/documents/{found[0][0]}").status_code == 404
    # HR issuing afterwards returns the same statement for Emma (idempotent) and adds Ivan's.
    r = w["acme"].post(f"{PAY}/runs/{settlement}/documents/final-settlement")
    assert r.status_code == 201 and found[0][0] in [i["id"] for i in r.json()["items"]] and len(r.json()["items"]) == 2
    # A regular run named in the event is never a settlement: the job fails permanently, nothing is issued.
    regular, _ = emma_run(w, start=date(2026, 11, 1))
    with session(w["cid"]) as (db, _):
        outbox.emit(db, "payroll.final_settlement.finalized", {"case_id": 2, "run_id": regular, "employee_id": emma_id})
        db.commit()
    assert sql("SELECT count(*) FROM documents WHERE doc_type = 'FINAL_SETTLEMENT_STATEMENT'")[0][0] == 2
    assert sql("SELECT status FROM jobs WHERE type = 'documents.final_settlement_statement' ORDER BY id")[-1] == (
        "FAILED",)


def test_zero_net_payslip_is_published_without_payment(docs_world):
    """Nothing to pay: the payslip follows ON_FINALIZE whatever the trigger (no payment evidence can ever come)."""
    w = docs_world
    set_setting(w["cid"], "payslip.publish_trigger", "ON_SETTLED")
    lines = (("BASIC", "EARNING", "1000.00", {}), ("LOAN_RECOVERY", "DEDUCTION", "1000.00", {}))
    run_id, rows = emma_run(w, lines=lines)
    generate(w["cid"], run_id)
    content = content_of(payslip_of(rows[w["emp"]["Emma"]["id"]])[0])
    assert content["totals"]["net"] == "0.00" and content["payment"]["key"] == "NO_PAYMENT"
    assert payslip_of(rows[w["emp"]["Emma"]["id"]])[1] == "PUBLISHED"


# ---------------------------------------------------------------------------
# End to end with the real run services (calculation, approval, finalization, outbox)
# ---------------------------------------------------------------------------
def test_ec_doc_04_real_finalization_generates_payslips_through_the_outbox(monkeypatch, fin_world):
    """A run calculated by the real engine, approved and finalized by the real services: the one
    ``payroll.run.finalized`` event generates and publishes one payslip per employee whose totals are the
    finalized figures, with the I-07 fixture badge of the IN fixture pack."""
    from app.payroll.services import outbox, publication
    from tests.payroll.test_run_finalize import approved_run, finalize

    w = fin_world
    # fin_world silences every subscriber of payroll.run.finalized; documents are re-attached alone here.
    monkeypatch.setitem(outbox.SUBSCRIBERS, "payroll.run.finalized", (publication.RUN_FINALIZED_JOB,))
    run = approved_run(w, kind="IN_FIXTURE")
    finalize(w["acme"], run["id"])
    rows = sql("SELECT id, employee_id, net, gross, currency FROM payroll_run_employees WHERE run_id = :r "
               "AND status = 'CALCULATED' ORDER BY id", r=run["id"])
    assert rows
    for row_id, employee_id, net, gross, currency in rows:
        doc = payslip_of(row_id)
        assert doc is not None and doc[1] in ("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED"), doc
        content = content_of(doc[0])
        assert content["currency"] == currency and content["employee"]["id"] == employee_id
        assert content["totals"]["net"] == f"{net:.2f}" and content["totals"]["gross"] == f"{gross:.2f}"
        assert "FIXTURE — not law" in content["labels"]
    assert sql("SELECT status FROM jobs WHERE type = 'documents.run_finalized'") == [("SUCCEEDED",)]
    # A second finalize call (idempotent replay) never issues a second set of payslips.
    finalize(w["acme"], run["id"])
    assert sql("SELECT count(*) FROM documents WHERE run_id = :r AND doc_type = 'PAYSLIP_V2'",
               r=run["id"])[0][0] == len(rows)
