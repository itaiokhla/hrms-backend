"""Commit hooks and SAVEPOINTs (F-72 (2), same SQLAlchemy 2.0 behaviour as F-68 (1)).

SQLAlchemy emits ``after_commit`` when a ``begin_nested`` SAVEPOINT is released, while the enclosing transaction
is still open. Two hooks acted on that event as if the data were committed:

* ``documents.store_bytes`` forgot its clean-up list, so a stored PDF stayed on disk when the outer transaction
  later rolled back;
* the legacy ``payroll_service`` payslip e-mail (``once=True``) went out at the savepoint release even when the
  outer transaction then rolled back.

Each test below fails without its fix.
"""

from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from app.core import storage
from app.database.db import SessionLocal, bind_tenant
from app.payroll.services import documents
from app.services import payroll_service

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@contextmanager
def tenant(company_id):
    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.close()


def _exists(company_id, path):
    try:
        storage.resolve(company_id, path)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- documents


def test_a_file_stored_before_a_released_savepoint_is_removed_when_the_outer_transaction_rolls_back(three_companies):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        before = documents.store_bytes(db, company, PDF)
        with db.begin_nested():
            inside = documents.store_bytes(db, company, PDF)
        # The savepoint release emitted after_commit; nothing is committed yet.
        assert _exists(company, before) and _exists(company, inside)
        db.rollback()
    assert not _exists(company, before) and not _exists(company, inside)


def test_files_stay_after_the_outer_commit_even_with_savepoints(three_companies):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        before = documents.store_bytes(db, company, PDF)
        with db.begin_nested():
            inside = documents.store_bytes(db, company, PDF)
        db.commit()
        # A later rollback in the same session must not touch files of the committed transaction.
        db.rollback()
    try:
        assert _exists(company, before) and _exists(company, inside)
    finally:
        storage.delete(company, before)
        storage.delete(company, inside)


def test_files_are_scoped_per_tenant_folder(three_companies):
    """Isolation: a file stored for tenant A lives under A's folder and cannot be resolved for tenant B."""
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db:
        path = documents.store_bytes(db, a, PDF)
        db.commit()
    try:
        assert _exists(a, path) and not _exists(b, path)
    finally:
        storage.delete(a, path)


# ------------------------------------------------------------------------------------- legacy payslip e-mail


@pytest.fixture
def sent(monkeypatch):
    calls = []
    from app.services import email_branding
    from app.services.email_service import EmailService

    monkeypatch.setattr(email_branding, "link", lambda company_id, path: f"https://example.test{path}")
    monkeypatch.setattr(EmailService, "send_template",
                        staticmethod(lambda key, to, context, company_id: calls.append((key, to, company_id))))
    return calls


def _queue(db, company_id):
    payroll = SimpleNamespace(company_id=company_id, salary_month=date(2026, 9, 1))
    employee = SimpleNamespace(first_name="Ana", full_name="Ana Example", email="ana@example.test")
    payroll_service._queue_payslip_email(db, payroll, employee)


def test_legacy_payslip_email_waits_for_the_outer_commit_and_is_sent_once(three_companies, sent):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        db.connection()  # begin the outer transaction
        _queue(db, company)
        with db.begin_nested():
            pass
        assert sent == []  # a released savepoint is not a commit
        db.commit()
        assert sent == [("payroll.payslip_published", "ana@example.test", company)]
        db.connection()
        db.commit()  # a later commit of the same session sends nothing again
    assert len(sent) == 1


def test_legacy_payslip_email_is_cancelled_when_the_outer_transaction_rolls_back(three_companies, sent):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        db.connection()
        _queue(db, company)
        with db.begin_nested():
            pass
        db.rollback()
        db.connection()
        db.commit()  # the next transaction of the session must not carry the cancelled e-mail
    assert sent == []


def test_legacy_payslip_email_queued_in_a_rolled_back_savepoint_is_never_sent(three_companies, sent):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        db.connection()
        savepoint = db.begin_nested()
        _queue(db, company)
        savepoint.rollback()
        db.commit()
    assert sent == []
