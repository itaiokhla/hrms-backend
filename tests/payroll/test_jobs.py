"""Background jobs: enqueue idempotency, inline/thread/worker execution, tenant binding, retries, reaper,
progress, cancellation, GET /api/v1/jobs/{id} authorization and log redaction.

3.9 §6, 3.5 §16, decision I-09; EC-RUN-02, EC-REL-01, EC-SEC-01, EC-SEC-02, EC-SEC-08.
"""

import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core import clock
from app.database.db import SessionLocal, TenantContextError, bind_tenant, current_company_id, system_session
from app.models.employee import Employee
from app.models.jobs import Job
from app.payroll.services import jobs
from app.services.audit_log_service import AuditLogService
from tests.helpers import create_employee, invite_and_activate

FUTURE = timedelta(days=1)
CALLS = []


@jobs.register("testjobs.echo")
def _echo(ctx):
    CALLS.append(("echo", ctx.job_id, ctx.company_id, dict(ctx.payload)))
    return {"echo": ctx.payload.get("value")}


@jobs.register("testjobs.tenant_probe")
def _tenant_probe(ctx):
    return {
        "bound": current_company_id(ctx.db),
        "setting": ctx.db.execute(text("SELECT current_setting('app.company_id', true)")).scalar(),
        "rls_rows": ctx.db.execute(text("SELECT count(*) FROM employees")).scalar(),
        "orm_rows": ctx.db.query(Employee).count(),
    }


@jobs.register("testjobs.platform_probe", platform=True)
def _platform_probe(ctx):
    return {"bound": current_company_id(ctx.db), "companies": ctx.db.execute(text("SELECT count(*) FROM companies")).scalar()}


@jobs.register("testjobs.flaky")
def _flaky(ctx):
    raise RuntimeError(
        "boom account_number=123456789012 iban GB82WEST12345698765432 salary: 50000.00\n"
        "DETAIL:  Key (company_id, key)=(7, secret-value) already exists."
    )


@jobs.register("testjobs.permanent")
def _permanent(ctx):
    raise jobs.PermanentJobError("the run no longer exists")


@jobs.register("testjobs.marker")
def _marker(ctx):
    AuditLogService.log(ctx.db, None, "Payroll Runs", "TEST_MARKER", f"job {ctx.job_id}", "job", ctx.job_id,
                        company_id=ctx.company_id, actor_type="system")
    return {"marked": True}


@jobs.register("testjobs.progress")
def _progress(ctx):
    for done in range(1, 4):
        ctx.progress(done, 3)
    return {"chunks": 3}


@jobs.register("testjobs.cancel_self")
def _cancel_self(ctx):
    AuditLogService.log(ctx.db, None, "Payroll Runs", "TEST_MARKER", "before cancel", "job", ctx.job_id,
                        company_id=ctx.company_id, actor_type="system")
    with tenant(ctx.company_id) as other:
        jobs.cancel(other, ctx.job_id, company_id=ctx.company_id)
        other.commit()
    ctx.progress(1, 2)  # raises JobInterrupted
    return {"unreachable": True}


@jobs.register("testjobs.parent")
def _parent(ctx):
    child = jobs.enqueue(ctx.db, company_id=ctx.company_id, type="testjobs.echo", payload={"value": "child"},
                         parent_job_id=ctx.job_id, idempotency_key=f"child-of-{ctx.job_id}")
    return {"child_job_id": child.id}


@contextmanager
def tenant(company_id):
    db = SessionLocal()
    bind_tenant(db, company_id)
    try:
        yield db
    finally:
        db.close()


def _enqueue(company_id, job_type="testjobs.echo", *, payload=None, **kwargs):
    with tenant(company_id) as db:
        job = jobs.enqueue(db, company_id=company_id, type=job_type, payload=payload or {}, **kwargs)
        db.commit()
        return job.id


def _job(job_id) -> Job:
    with system_session() as db:
        return db.get(Job, job_id)


def _markers():
    with system_session() as db:
        return db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'TEST_MARKER'")).scalar()


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    yield
    CALLS.clear()


@pytest.fixture
def frozen(monkeypatch):
    """Moves app.core.clock (jobs read time only through it)."""
    state = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(clock, "utcnow", lambda: state["now"])
    return state


def test_inline_mode_runs_the_job_after_the_enqueuing_transaction_commits(three_companies):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        job = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "hello"})
        assert CALLS == []  # nothing runs before commit
        db.commit()
    assert CALLS == [("echo", job.id, company, {"value": "hello"})]
    row = _job(job.id)
    assert (row.status, row.result, row.attempts, row.locked_by) == ("SUCCEEDED", {"echo": "hello"}, 1, None)


def test_inline_jobs_queued_before_a_savepoint_run_after_the_outer_commit(three_companies):
    """Regression (F-68): SQLAlchemy emits after_commit when a SAVEPOINT is released; the inline runner used to
    claim then (the outer transaction still open, so nothing was visible) and dropped the job for good. A job
    queued before a released savepoint, inside one, or after one runs once, after the outermost commit."""
    company = three_companies["A"].company_id
    with tenant(company) as db:
        before = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "before"})
        with db.begin_nested():
            inside = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "inside"})
        assert CALLS == []  # nothing runs when the savepoint is released
        with db.begin_nested():
            pass
        after = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "after"})
        assert CALLS == []
        db.commit()
    assert sorted(c[1] for c in CALLS) == sorted([before.id, inside.id, after.id])
    assert {_job(j.id).status for j in (before, inside, after)} == {"SUCCEEDED"}


def test_a_job_queued_in_a_rolled_back_savepoint_never_runs(three_companies):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        kept = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "kept"})
        savepoint = db.begin_nested()
        dropped = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "dropped"})
        dropped_id = dropped.id
        savepoint.rollback()
        db.commit()
    assert [c[1] for c in CALLS] == [kept.id] and _job(dropped_id) is None


def test_a_rolled_back_enqueue_leaves_no_job_and_runs_nothing(three_companies):
    company = three_companies["A"].company_id
    with tenant(company) as db:
        job = jobs.enqueue(db, company_id=company, type="testjobs.echo", payload={"value": "x"})
        job_id = job.id
        db.rollback()
    assert CALLS == [] and _job(job_id) is None


def test_ec_run_02_same_company_type_and_key_returns_the_first_job(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    first = _enqueue(a, idempotency_key="calc-run-7", payload={"value": "1"})
    again = _enqueue(a, idempotency_key="calc-run-7", payload={"value": "2"})
    other_type = _enqueue(a, "testjobs.marker", idempotency_key="calc-run-7")
    other_company = _enqueue(b, idempotency_key="calc-run-7", payload={"value": "3"})
    assert first == again
    assert len({first, other_type, other_company}) == 3
    assert [c[3] for c in CALLS if c[0] == "echo"] == [{"value": "1"}, {"value": "3"}]  # the repeat ran nothing
    with system_session() as db:
        assert db.query(Job).filter(Job.idempotency_key == "calc-run-7").count() == 3


def test_ec_sec_01_run_job_binds_the_jobs_tenant_so_rls_applies(three_companies):
    admin_a = three_companies["A"]
    for name in ("Asha", "Bilal"):
        create_employee(admin_a, name)
    a = admin_a.company_id
    job_id = _enqueue(a, "testjobs.tenant_probe")
    result = _job(job_id).result
    with system_session() as db:
        own = db.execute(text("SELECT count(*) FROM employees WHERE company_id = :c"), {"c": a}).scalar()
        everyone = db.execute(text("SELECT count(*) FROM employees")).scalar()
    assert everyone > own == 3
    assert result == {"bound": a, "setting": str(a), "rls_rows": own, "orm_rows": own}


def test_platform_jobs_run_on_the_system_session_and_types_cannot_cross(three_companies):
    a = three_companies["A"].company_id
    with system_session() as db:
        job = jobs.enqueue(db, company_id=None, type="testjobs.platform_probe", payload={})
        db.commit()
        platform_id = job.id
    assert _job(platform_id).result == {"bound": None, "companies": 3}
    mismatched = _enqueue(a, "testjobs.platform_probe")
    row = _job(mismatched)
    assert row.status == "FAILED" and "platform job type" in row.error


def test_enqueue_refuses_another_tenant_and_unbound_sessions(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as db, pytest.raises(TenantContextError):
        jobs.enqueue(db, company_id=b, type="testjobs.echo", payload={})
    db = SessionLocal()
    try:
        with pytest.raises(TenantContextError):
            jobs.enqueue(db, company_id=a, type="testjobs.echo", payload={})
    finally:
        db.close()


@pytest.mark.parametrize("payload", [
    {"amount": "100.00"}, {"net_pay": "5"}, {"iban": "GB82WEST12345698765432"}, {"rate": 1.5},
    {"value": __import__("decimal").Decimal("1")}, {"note": "x" * 201}, {"nested": {"salary": "1"}},
])
def test_ec_sec_08_payloads_carry_ids_only(three_companies, payload):
    a = three_companies["A"].company_id
    with tenant(a) as db, pytest.raises(ValueError):
        jobs.enqueue(db, company_id=a, type="testjobs.echo", payload=payload)


def test_ec_sec_08_id_keys_of_sensitive_objects_are_allowed(three_companies):
    a = three_companies["A"].company_id
    job_id = _enqueue(a, payload={"funding_account_id": 3, "bank_account_ids": [1, 2], "pay_group_id": 4,
                                  "run": {"id": 9, "status": "CALCULATING"}})
    assert _job(job_id).status == "SUCCEEDED"


def test_failures_retry_with_exponential_backoff_then_become_dead(three_companies, frozen):
    a = three_companies["A"].company_id
    start = frozen["now"]
    job_id = _enqueue(a, "testjobs.flaky", max_attempts=3)
    row = _job(job_id)
    assert (row.status, row.attempts) == ("QUEUED", 1)
    assert row.run_at == start + timedelta(seconds=30)
    assert jobs.run_pending() == 0  # not due yet
    frozen["now"] = start + timedelta(seconds=31)
    assert jobs.run_pending() == 1
    row = _job(job_id)
    assert (row.status, row.attempts, row.run_at) == ("QUEUED", 2, frozen["now"] + timedelta(seconds=60))
    frozen["now"] += timedelta(seconds=61)
    jobs.run_pending()
    row = _job(job_id)
    assert (row.status, row.attempts, row.locked_by) == ("DEAD", 3, None)
    assert [jobs.backoff(n).total_seconds() for n in (1, 2, 3, 8, 20)] == [30, 60, 120, 3600, 3600]


def test_ec_sec_08_job_errors_are_stored_redacted(three_companies):
    job_id = _enqueue(three_companies["A"].company_id, "testjobs.flaky", max_attempts=1)
    row = _job(job_id)
    assert row.status == "DEAD"
    for secret in ("123456789012", "GB82WEST12345698765432", "50000.00", "secret-value"):
        assert secret not in row.error
    assert row.error.startswith("RuntimeError: boom")


def test_permanent_errors_fail_at_once(three_companies):
    row = _job(_enqueue(three_companies["A"].company_id, "testjobs.permanent"))
    assert (row.status, row.attempts) == ("FAILED", 1)
    assert "no longer exists" in row.error


def test_unknown_job_types_are_retried_then_dead(three_companies):
    row = _job(_enqueue(three_companies["A"].company_id, "testjobs.nobody_handles_this", max_attempts=1))
    assert row.status == "DEAD" and "No handler" in row.error


def test_ec_rel_01_reaper_requeues_jobs_whose_heartbeat_is_stale(three_companies, frozen):
    a = three_companies["A"].company_id
    start = frozen["now"]
    # Not due yet, so the inline runner leaves it for the worker that claims it and then crashes.
    later = _enqueue(a, "testjobs.marker", run_at=start + FUTURE)
    frozen["now"] = start + FUTURE
    crashed = jobs.claim("crashed-worker", ids=[later])
    assert [c.id for c in crashed] == [later] and _job(later).status == "RUNNING"
    frozen["now"] += timedelta(minutes=4, seconds=59)
    assert jobs.reap() == 0
    frozen["now"] += timedelta(seconds=2)
    assert jobs.reap() == 1
    row = _job(later)
    assert (row.status, row.locked_by) == ("QUEUED", None) and "stopped responding" in row.error
    assert jobs.run_pending() == 1
    assert _job(later).status == "SUCCEEDED" and _markers() == 1


def test_ec_rel_01_a_worker_that_lost_its_job_commits_nothing(three_companies, frozen):
    a = three_companies["A"].company_id
    start = frozen["now"]
    job_id = _enqueue(a, "testjobs.marker", run_at=start + FUTURE)
    frozen["now"] = start + FUTURE
    slow = jobs.claim("slow-worker", ids=[job_id])[0]
    frozen["now"] += timedelta(minutes=6)
    jobs.reap()
    assert jobs.run_pending(worker_id="healthy-worker") == 1
    assert jobs.run_job(slow, "slow-worker") == "INTERRUPTED"
    row = _job(job_id)
    assert (row.status, row.attempts) == ("SUCCEEDED", 2)
    assert _markers() == 1  # the slow worker's handler effect was rolled back


def test_reaper_retires_a_stale_job_without_attempts_left(three_companies, frozen):
    a = three_companies["A"].company_id
    job_id = _enqueue(a, "testjobs.marker", run_at=frozen["now"] + FUTURE, max_attempts=1)
    frozen["now"] += FUTURE
    jobs.claim("crashed-worker", ids=[job_id])
    frozen["now"] += timedelta(minutes=10)
    jobs.reap()
    row = _job(job_id)
    assert row.status == "DEAD" and "no attempts left" in row.error


def test_progress_is_recorded_and_reported(three_companies, app):
    admin = three_companies["A"]
    job_id = _enqueue(admin.company_id, "testjobs.progress", created_by_user_id=admin.me()["user"]["id"])
    body = admin.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "SUCCEEDED" and body["finished"] is True
    assert body["progress"] == {"done": 3, "total": 3, "percent": 100}
    assert body["result"] == {"chunks": 3} and body["error"] is None
    assert "payload" not in body


def test_cancel_stops_a_running_job_and_rolls_back_its_work(three_companies):
    a = three_companies["A"].company_id
    job_id = _enqueue(a, "testjobs.cancel_self")
    row = _job(job_id)
    assert row.status == "CANCELLED" and row.result is None
    assert _markers() == 0


def test_cancel_queued_jobs_with_children_and_refuse_finished_ones(three_companies, frozen):
    from app.core.errors import AppError

    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    parent = _enqueue(a, "testjobs.echo", run_at=frozen["now"] + FUTURE)
    child = _enqueue(a, "testjobs.echo", run_at=frozen["now"] + FUTURE, parent_job_id=parent)
    done = _enqueue(a, "testjobs.echo")
    with tenant(a) as db:
        with pytest.raises(AppError) as foreign:
            jobs.cancel(db, parent, company_id=b)
        assert foreign.value.status_code == 404
        db.rollback()
        assert jobs.cancel(db, parent, company_id=a).status == "CANCELLED"
        db.commit()
        with pytest.raises(AppError) as finished:
            jobs.cancel(db, done, company_id=a)
        assert finished.value.status_code == 409 and finished.value.code == "JOB_FINISHED"
    assert _job(child).status == "CANCELLED"
    frozen["now"] += FUTURE * 2
    assert jobs.run_pending() == 0


def test_child_jobs_queued_by_a_handler_run_after_the_parent_commits(three_companies):
    a = three_companies["A"].company_id
    parent = _enqueue(a, "testjobs.parent")
    child_id = _job(parent).result["child_job_id"]
    child = _job(child_id)
    assert (child.parent_job_id, child.status, child.result) == (parent, "SUCCEEDED", {"echo": "child"})


def test_ec_sec_02_get_job_is_limited_to_the_creator_permission_holders_and_the_tenant(three_companies, app):
    founder, other_company = three_companies["A"], three_companies["B"]
    founder_id = founder.me()["user"]["id"]
    clerk = invite_and_activate(app, founder, create_employee(founder, "Clerk"), ["employee"])
    second_admin = invite_and_activate(app, founder, create_employee(founder, "Admin2"), ["company_admin"])
    own = _enqueue(founder.company_id, created_by_user_id=founder_id)
    clerks = _enqueue(founder.company_id, created_by_user_id=clerk.user_id)
    readable = _enqueue(founder.company_id, created_by_user_id=clerk.user_id, read_permission="payroll:run:read")

    assert founder.get(f"/api/v1/jobs/{own}").status_code == 200
    assert clerk.get(f"/api/v1/jobs/{clerks}").status_code == 200
    assert clerk.get(f"/api/v1/jobs/{own}").status_code == 404
    # The founder holds payroll:run:read company-wide through payroll_manager; a plain administrator does not (EC-SEC-03).
    assert founder.get(f"/api/v1/jobs/{readable}").status_code == 200
    assert founder.get(f"/api/v1/jobs/{clerks}").status_code == 404
    assert second_admin.get(f"/api/v1/jobs/{readable}").status_code == 404
    for job_id in (own, clerks, readable):
        r = other_company.get(f"/api/v1/jobs/{job_id}")
        assert (r.status_code, r.json()["code"]) == (404, "NOT_FOUND")
    assert founder.get("/api/v1/jobs/999999").status_code == 404
    with system_session() as db:
        db.add(Job(company_id=None, type="testjobs.platform_probe", payload={}, status="QUEUED",
                   run_at=datetime.now(timezone.utc) + FUTURE, created_by_user_id=founder_id))
        db.commit()
        platform_job = db.query(Job).filter(Job.company_id.is_(None)).order_by(Job.id.desc()).first().id
    assert founder.get(f"/api/v1/jobs/{platform_job}").status_code == 404  # platform jobs are never tenant-readable


def test_f21_get_job_sends_an_etag_and_answers_304_until_the_job_changes(three_companies, frozen):
    founder, other_company = three_companies["A"], three_companies["B"]
    founder_id = founder.me()["user"]["id"]
    job_id = _enqueue(founder.company_id, created_by_user_id=founder_id, run_at=frozen["now"] + FUTURE)
    url = f"/api/v1/jobs/{job_id}"
    first = founder.get(url)
    assert first.status_code == 200 and first.json()["status"] == "QUEUED"
    etag = first.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"') and len(etag) > 10
    assert first.headers["cache-control"] == "private, no-cache"
    # Unchanged: 304 with no body and the same tag, for the plain, weak, listed and wildcard forms.
    for header in (etag, f"W/{etag}", f'"stale", {etag}', "*"):
        r = founder.get(url, headers={"If-None-Match": header})
        assert r.status_code == 304, header
        assert r.content == b"" and r.headers["etag"] == etag
    # A different tag gets the full body again.
    r = founder.get(url, headers={"If-None-Match": '"something-else"'})
    assert r.status_code == 200 and r.json()["id"] == job_id and r.headers["etag"] == etag
    # A 304 never leaks a job to another tenant: access is checked first.
    r = other_company.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 404
    r = other_company.get(url, headers={"If-None-Match": "*"})
    assert r.status_code == 404
    # Once the job runs, the representation changes, so the old tag no longer matches.
    frozen["now"] += FUTURE * 2
    assert jobs.run_pending(ids=[job_id]) == 1
    changed = founder.get(url, headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.json()["status"] == "SUCCEEDED"
    assert changed.headers["etag"] != etag
    assert founder.get(url, headers={"If-None-Match": changed.headers["etag"]}).status_code == 304


def test_get_job_requires_a_session(three_companies, app):
    from tests.helpers import new_client

    job_id = _enqueue(three_companies["A"].company_id)
    assert new_client(app).get(f"/api/v1/jobs/{job_id}").status_code == 401


def test_worker_tick_and_embedded_thread_run_due_jobs(three_companies, frozen, monkeypatch):
    from app import worker

    a = three_companies["A"].company_id
    start = frozen["now"]
    first = _enqueue(a, run_at=start + FUTURE, payload={"value": "tick"})
    second = _enqueue(a, run_at=start + FUTURE, payload={"value": "thread"})
    frozen["now"] = start + FUTURE
    w = worker.Worker(kind="test")
    w.batch = 1
    assert w.tick() == 1
    assert {_job(first).status, _job(second).status} == {"SUCCEEDED", "QUEUED"}
    thread = worker.start_embedded(poll_seconds=0.05)
    try:
        assert worker.start_embedded() is thread
        deadline = time.monotonic() + 20
        while _job(second).status != "SUCCEEDED" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        worker.stop_embedded()
    assert not thread.is_alive()
    assert _job(first).status == _job(second).status == "SUCCEEDED"


def test_jobs_mode_validation(monkeypatch):
    from app import worker
    from app.config import settings

    monkeypatch.setattr(settings, "jobs_mode", "inline")
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(RuntimeError):
        worker.validate_mode()
    monkeypatch.setattr(settings, "jobs_mode", "celery")
    monkeypatch.setattr(settings, "environment", "development")
    with pytest.raises(RuntimeError):
        worker.validate_mode()
    monkeypatch.setattr(settings, "jobs_mode", "worker")
    assert worker.validate_mode() == "worker"


def test_ec_sec_08_engines_hide_statement_parameters():
    from app.database.db import engine, system_engine

    assert engine.hide_parameters is True and system_engine.hide_parameters is True


def test_ec_sec_08_log_redaction_filter_scrubs_messages_and_tracebacks():
    try:
        raise ValueError("insert failed: [parameters: {'iban': 'DE89370400440532013000', 'amount': '1200.50'}]")
    except ValueError:
        record = logging.LogRecord("hrms", logging.ERROR, __file__, 1, "Paying %s to account_number=%s", (
            "AED 1,200", "123456789012"), sys.exc_info())
    jobs.RedactingFilter().filter(record)
    formatted = logging.Formatter().format(record)
    for secret in ("123456789012", "DE89370400440532013000", "1200.50"):
        assert secret not in formatted
    assert "account_number=[redacted]" in formatted and "[parameters: redacted]" in formatted
    assert jobs.redact("job 42 for run 7 on 2026-10-21 12:00:00") == "job 42 for run 7 on 2026-10-21 12:00:00"


def test_ec_sec_08_access_log_query_strings_are_redacted_and_still_format():
    """uvicorn's AccessFormatter unpacks record.args: the filter must keep their shape."""
    from uvicorn.logging import AccessFormatter

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', (
        "10.0.0.1:5000", "GET", "/api/v1/payroll/calculate?bonus=5000&account_number=123456789012", "1.1", 200), None)
    jobs.RedactingFilter().filter(record)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert "5000&" not in line and "123456789012" not in line
    assert "bonus=[redacted]&account_number=[redacted]" in line and line.endswith("200 OK")
    harmless = logging.LogRecord("hrms", logging.INFO, __file__, 1, "job %s for run %s", (42, 7), None)
    jobs.RedactingFilter().filter(harmless)
    assert harmless.args == (42, 7)
    assert jobs.redact(jobs.redact("amount=500")) == "amount=[redacted]"  # idempotent


def test_ec_sec_08_log_redaction_is_installed_on_handlers():
    handler = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        jobs.install_log_redaction()
        jobs.install_log_redaction()
        assert sum(isinstance(f, jobs.RedactingFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)
