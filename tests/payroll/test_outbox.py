"""Transactional outbox: events recorded with the business change, dispatched into jobs exactly once.

3.9 §6, 3.5 §16; EC-REL-02, EC-REL-03, EC-SEC-01, EC-SEC-08.
"""

from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.database.db import SessionLocal, SystemSessionLocal, TenantContextError, bind_tenant, system_session
from app.models.jobs import Job, OutboxEvent
from app.payroll.services import jobs, outbox

DELIVERED = []


@jobs.register("testoutbox.deliver")
def _deliver(ctx):
    DELIVERED.append((ctx.company_id, ctx.payload["document_id"]))
    return {"delivered": ctx.payload["document_id"]}


@jobs.register("testoutbox.archive")
def _archive(ctx):
    DELIVERED.append(("archive", ctx.payload["document_id"]))


outbox.subscribe("testoutbox.document_issued", "testoutbox.deliver")
outbox.subscribe("testoutbox.document_issued", "testoutbox.archive")


@pytest.fixture(autouse=True)
def _reset():
    DELIVERED.clear()
    yield
    DELIVERED.clear()


@contextmanager
def tenant(company_id):
    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.close()


def _jobs_of(job_type):
    with system_session() as db:
        return db.query(Job).filter(Job.type == job_type).order_by(Job.id).all()


def _events():
    with system_session() as db:
        return db.query(OutboxEvent).order_by(OutboxEvent.id).all()


def test_ec_rel_02_an_event_becomes_each_subscriber_job_once_after_commit(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as db:
        event = outbox.emit(db, "testoutbox.document_issued", {"document_id": 41})
        assert DELIVERED == []
        db.commit()
    assert sorted(DELIVERED, key=str) == sorted([(a, 41), ("archive", 41)], key=str)
    [stored] = _events()
    assert stored.id == event.id and stored.dispatched_at is not None and stored.attempts == 1
    [deliver] = _jobs_of("testoutbox.deliver")
    assert (deliver.company_id, deliver.idempotency_key, deliver.status) == (
        a, f"outbox:{event.id}:testoutbox.deliver", "SUCCEEDED")
    # Dispatching again (a redelivered dispatcher job, EC-REL-03) creates nothing new.
    with system_session() as db:
        assert outbox.dispatch_pending(db) == {"dispatched": 0, "unhandled": 0}
        db.commit()
    assert len(_jobs_of("testoutbox.deliver")) == 1 and len(DELIVERED) == 2


def test_ec_rel_02_a_rolled_back_business_change_emits_nothing(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as db:
        outbox.emit(db, "testoutbox.document_issued", {"document_id": 7})
        db.rollback()
    assert _events() == [] and _jobs_of(outbox.DISPATCH_JOB) == [] and DELIVERED == []


def test_ec_rel_02_a_dispatch_that_fails_before_commit_is_retried_without_duplicates(three_companies, monkeypatch):
    a = three_companies["A"].company_id
    with system_session() as db:
        # Record the event without its dispatcher job, as if the dispatcher crashed.
        db.add(OutboxEvent(company_id=a, topic="testoutbox.document_issued", payload={"document_id": 5}, attempts=0))
        db.commit()
    with system_session() as db:
        outbox.dispatch_pending(db)
        db.rollback()  # crash before commit
    assert _jobs_of("testoutbox.deliver") == [] and _events()[0].dispatched_at is None
    with system_session() as db:
        assert outbox.dispatch_pending(db)["dispatched"] == 1
        db.commit()
    with system_session() as db:
        assert outbox.dispatch_pending(db)["dispatched"] == 0
        db.commit()
    assert [j.status for j in _jobs_of("testoutbox.deliver")] == ["SUCCEEDED"]
    assert DELIVERED.count((a, 5)) == 1


def test_ec_rel_02_concurrent_dispatchers_skip_each_others_rows(three_companies):
    a = three_companies["A"].company_id
    with system_session() as db:
        db.add(OutboxEvent(company_id=a, topic="testoutbox.document_issued", payload={"document_id": 1}, attempts=0))
        db.commit()
    first, second = SystemSessionLocal(), SystemSessionLocal()
    try:
        assert outbox.dispatch_pending(first)["dispatched"] == 1  # holds the row lock
        assert outbox.dispatch_pending(second)["dispatched"] == 0  # SKIP LOCKED
        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()
    assert len(_jobs_of("testoutbox.deliver")) == 1


def test_events_nobody_handles_stay_pending_with_an_error(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as db:
        outbox.emit(db, "testoutbox.unknown_topic", {"document_id": 3})
        db.commit()
    [event] = _events()
    assert event.dispatched_at is None and event.attempts == 1 and "No job type" in event.last_error
    assert [j.status for j in _jobs_of(outbox.DISPATCH_JOB)] == ["SUCCEEDED"]


def test_ec_sec_08_outbox_payloads_carry_ids_only(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as db:
        for payload in ({"amount": "10.00"}, {"account_number": "123"}, {"rate": 1.2}):
            with pytest.raises(ValueError):
                outbox.emit(db, "testoutbox.document_issued", payload)


def test_emit_needs_a_tenant_transaction():
    db = SessionLocal()
    try:
        with pytest.raises(TenantContextError):
            outbox.emit(db, "testoutbox.document_issued", {"document_id": 1})
    finally:
        db.close()


def test_ec_sec_01_outbox_rows_are_tenant_isolated(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db:
        outbox.emit(db, "testoutbox.unknown_topic", {"document_id": 1})
        db.commit()
    with tenant(b) as db:
        assert db.query(OutboxEvent).count() == 0
        assert db.execute(text("SELECT count(*) FROM outbox_events")).scalar() == 0
    with tenant(a) as db:
        assert db.query(OutboxEvent).count() == 1
