"""Payslip generation, publication and notification (3.6 §5-§7.1; EC-DOC-04/05/11/12/14/16).

* Generation. The outbox event ``payroll.run.finalized`` starts job ``documents.run_finalized``: one
  ``document_generation_tasks`` row per finalized run employee and document version, unique on
  (run_employee_id, doc_type, document_version) so a repeated event or a retried job never issues a
  payslip twice (EC-DOC-04). Every task runs in its own savepoint: a failure (an object-storage outage,
  a template error) never blocks the others; it is retried with backoff and becomes FAILED after
  ``MAX_TASK_ATTEMPTS``, and a partially written file is removed and never served (EC-DOC-16). "Retry
  failed" re-queues only the failures.
* Publication (``payslip.publish_trigger``, resolved for the run's legal entity and pay group):
  ON_FINALIZE publishes at once; ON_PAYMENT_SUBMITTED when the employee's payment instruction was
  submitted; ON_SETTLED when every instruction of the employee is settled with verified evidence (a
  failed payment keeps the payslip unpublished, EC-DOC-11); SCHEDULED at ``payslip.scheduled_publish_time``
  (entity time) on the pay date. A payslip with nothing to pay follows ON_FINALIZE. A sweep job re-checks
  the triggers until every payslip of the run is published; ``on_payment_progress`` lets the payment
  services re-check at once. HR may publish manually with a reason (PUBLISH_MANUALLY).
* Notification: exactly one per payslip version and channel (``document_notifications`` unique key,
  EC-DOC-05). In-app: "Your payslip for … is ready". E-mail (``payslip.email_notify``): a link and no
  amounts unless ``payslip.email_include_net_pay``; a PDF is attached only when
  ``payslip.email_attach_pdf`` and ``payslip.pdf_protection = EMPLOYEE_SECRET`` and the employee has set
  a PDF password, which encrypts the attachment (V-09, EC-DOC-14); otherwise the link alone is sent.
* Corrections: ``issue_revised_payslip`` issues the next version titled "Revised payslip — replaces …"
  and supersedes the original; ``reverse_run_documents`` marks the reversed run's payslips REVERSED
  and issues a reversal statement per employee (EC-DOC-12).

Services take (db, principal, ...) and never commit; job handlers run in the job's transaction.
"""

from __future__ import annotations

import html
import logging
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

import pytz
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings as app_settings
from app.core import clock
from app.core.errors import bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.employee import Employee
from app.models.org import LegalEntity
from app.models.pay_documents import (
    DocumentGenerationTask,
    DocumentNotification,
    PayDocument,
    PayslipPdfSecret,
)
from app.models.pay_runs import PayPeriod, PayrollRun, PayrollRunEmployee
from app.payroll import audit
from app.payroll import state_machines as sm
from app.payroll.money import amount_str
from app.payroll.services import documents, jobs, outbox, payslips
from app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

FINALIZED_TOPIC = "payroll.run.finalized"
REVERSED_TOPIC = "payroll.run.reversed"  # emitted by services.reversals
FINAL_SETTLEMENT_TOPIC = "payroll.final_settlement.finalized"  # emitted by services.final_settlement
RUN_FINALIZED_JOB = "documents.run_finalized"
RUN_REVERSED_JOB = "documents.run_reversed"
FINAL_SETTLEMENT_JOB = "documents.final_settlement_statement"
GENERATE_JOB = "documents.generate_payslips"
SWEEP_JOB = "documents.publication_sweep"
EMAIL_JOB = "documents.payslip_email"
PAYSLIP = documents.PAYSLIP
MAX_TASK_ATTEMPTS = 3
SWEEP_EVERY = timedelta(hours=1)
SWEEP_UNTIL = timedelta(days=120)
PUBLISHED_STATES = ("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED", "VIEWED")
PRIVACY_SENTENCE = "For your privacy this email does not include amounts."
EMAIL_TEMPLATE = "payroll.payslip_published"
SECRET_PURPOSE = "PAYROLL_PII"
SECRET_AAD = b"payslip_pdf_secrets.secret"
IN_FLIGHT_OR_LATER = frozenset(("SUBMITTED", "ACKNOWLEDGED", "PROCESSING", "PARTIALLY_SETTLED", "SETTLED",
                                "RECONCILED", "RETURNED", "REVERSED", "FAILED", "REJECTED"))
FULLY_SETTLED = frozenset(("SETTLED", "RECONCILED"))


# --------------------------------------------------------------------------- helpers


def _run(db: Session, company_id: int, run_id: int, *, lock: bool = False) -> PayrollRun:
    query = db.query(PayrollRun).filter(PayrollRun.company_id == company_id, PayrollRun.id == run_id)
    if lock:
        query = query.with_for_update()
    run = query.one_or_none()
    if run is None:
        raise not_found("Payroll run")
    return run


def _period(db: Session, run: PayrollRun) -> PayPeriod:
    return db.query(PayPeriod).filter(PayPeriod.company_id == run.company_id, PayPeriod.id == run.pay_period_id).one()


def reversed_run_employee_ids(db: Session, run: PayrollRun) -> set[int]:
    """Run employees of ``run`` covered by a finalized REVERSAL run (guard RUN_NOT_REVERSED, per employee).

    A reversal may select employees (3.5 §12): only the selected run employees are reversed, the others'
    payslips stay publishable. The selection comes from the reversal record (``payroll_reversals``); a
    finalized REVERSAL run without one covers the employees it carries.
    """
    from app.models.corrections import PayrollReversal

    reversal_runs = [rid for (rid,) in db.query(PayrollRun.id).filter(
        PayrollRun.company_id == run.company_id, PayrollRun.reverses_run_id == run.id,
        PayrollRun.status.in_(sm.RUN_FINALIZED_STATES))]
    if not reversal_runs:
        return set()
    covered: set[int] = set()
    recorded: set[int] = set()
    for reversal_run_id, selected in db.query(PayrollReversal.reversal_run_id, PayrollReversal.run_employee_ids).filter(
            PayrollReversal.company_id == run.company_id, PayrollReversal.original_run_id == run.id,
            PayrollReversal.reversal_run_id.in_(reversal_runs)):
        covered.update(int(i) for i in selected or [])
        recorded.add(reversal_run_id)
    unrecorded = [rid for rid in reversal_runs if rid not in recorded]
    if unrecorded:
        employees = {e for (e,) in db.query(PayrollRunEmployee.employee_id).filter(
            PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id.in_(unrecorded),
            PayrollRunEmployee.status != "EXCLUDED")}
        covered.update(rid for rid, employee_id in db.query(PayrollRunEmployee.id, PayrollRunEmployee.employee_id)
                       .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)
                       if employee_id in employees)
    return covered


def run_is_reversed(db: Session, run: PayrollRun) -> bool:
    """Guard RUN_NOT_REVERSED for the whole run: every finalized employee of ``run`` is reversed."""
    reversed_rows = reversed_run_employee_ids(db, run)
    if not reversed_rows:
        return False
    return all(row.id in reversed_rows for row in eligible_rows(db, run))


def _today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def _files_marker(db: Session) -> int:
    return len(db.info.get(documents._FILES_KEY) or [])


def _discard_files_since(db: Session, marker: int) -> None:
    from app.core import storage

    files = db.info.get(documents._FILES_KEY) or []
    for company_id, path in files[marker:]:
        storage.delete(company_id, path)
    del files[marker:]


def eligible_rows(db: Session, run: PayrollRun) -> list[PayrollRunEmployee]:
    """Run employees whose pay was finalized (a FULL hold, an exclusion or an error has no payslip)."""
    return (db.query(PayrollRunEmployee)
            .filter(PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id,
                    PayrollRunEmployee.status == "CALCULATED")
            .order_by(PayrollRunEmployee.id).all())


# --------------------------------------------------------------------------- generation (EC-DOC-04, EC-DOC-16)


def queue_tasks(db: Session, run: PayrollRun, *, document_version: int = 1) -> int:
    """One QUEUED task per eligible run employee; existing tasks are kept (idempotent). Returns the new count."""
    rows = eligible_rows(db, run)
    if not rows:
        return 0
    now = clock.utcnow()
    result = db.execute(
        insert(DocumentGenerationTask)
        .values([{"company_id": run.company_id, "run_id": run.id, "run_employee_id": row.id, "doc_type": PAYSLIP,
                  "document_version": document_version, "status": "QUEUED", "attempts": 0, "retries": 0,
                  "row_version": 1, "created_at": now, "updated_at": now} for row in rows])
        .on_conflict_do_nothing(index_elements=["company_id", "run_employee_id", "doc_type", "document_version"])
        .returning(DocumentGenerationTask.id)
    )
    return len(result.all())


def generate_payslip(db: Session, run: PayrollRun, row: PayrollRunEmployee, *, version: int = 1,
                     settings: Optional[Mapping[str, Any]] = None, title_key: str = "PAYSLIP",
                     replaces: Optional[PayDocument] = None, principal: Optional[Principal] = None) -> PayDocument:
    """Issue (or return) the payslip document of ``row`` at ``version`` (idempotent on the source key)."""
    existing = documents.find_version(db, run.company_id, PAYSLIP, payslips.source_key(row.id), version)
    if existing is not None:
        return existing
    period = _period(db, run)
    model = payslips.build(db, run, row, version=version, title_key=title_key,
                           replaces=replaces.number if replaces is not None else None, settings=settings)
    employee = db.query(Employee).filter(Employee.company_id == run.company_id, Employee.id == row.employee_id).one()
    title = ("Revised payslip" if title_key == "REVISED_PAYSLIP" else "Payslip") + f" — {model.content['period']['label']}"
    return documents.issue(
        db, principal, company_id=run.company_id, doc_type=PAYSLIP, legal_entity_id=run.legal_entity_id,
        pay_group_id=run.pay_group_id, subject_type="EMPLOYEE", subject_id=row.employee_id,
        source_key=payslips.source_key(row.id), content=model.content, title=title,
        file_stem=payslips.file_stem(employee, period.period_start), version=version,
        issue_date=_today(db, run.company_id), number_date=period.pay_date, employee_id=row.employee_id,
        run_id=run.id, run_employee_id=row.id, period=(period.period_start, period.period_end),
        source_refs={"run_id": run.id, "run_number": run.number, "run_employee_id": row.id,
                     "snapshot_id": row.snapshot_id, "calc_hash": row.calc_hash, "content_hash": row.content_hash},
        supersedes=replaces, template_version=model.template_version,
        template_policy_version_id=model.template_policy_version_id, locale=model.locale,
        languages=model.languages, labels=model.labels,
    )


def process_tasks(db: Session, run: PayrollRun, *, task_ids: Optional[Sequence[int]] = None) -> dict:
    """Generate every QUEUED task of the run, each in its own savepoint (partial failures never block the rest)."""
    query = db.query(DocumentGenerationTask).filter(
        DocumentGenerationTask.company_id == run.company_id, DocumentGenerationTask.run_id == run.id,
        DocumentGenerationTask.status == "QUEUED")
    if task_ids is not None:
        query = query.filter(DocumentGenerationTask.id.in_(list(task_ids)))
    tasks = query.order_by(DocumentGenerationTask.id).with_for_update(skip_locked=True).all()
    if not tasks:
        return {"generated": 0, "failed": 0, "retrying": 0}
    period = _period(db, run)
    try:
        values = payslips.payslip_settings(db, run, period)
    except Exception as exc:  # noqa: BLE001 - invalid payslip settings fail every task with the reason
        values = exc
    rows = {r.id: r for r in db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == run.company_id,
        PayrollRunEmployee.id.in_([t.run_employee_id for t in tasks]))}
    generated = failed = retrying = 0
    retry_ids: list[int] = []
    for task in tasks:
        task.attempts += 1
        task.row_version += 1
        marker = _files_marker(db)
        savepoint = db.begin_nested()
        try:
            if isinstance(values, Exception):
                raise values
            doc = generate_payslip(db, run, rows[task.run_employee_id], version=task.document_version,
                                   settings=values)
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - recorded on the task, retried, then FAILED
            savepoint.rollback()
            _discard_files_since(db, marker)
            task.last_error = f"{type(exc).__name__}: {exc}"[:500]
            if task.attempts >= MAX_TASK_ATTEMPTS:
                task.status = "FAILED"
                failed += 1
            else:
                retrying += 1
                retry_ids.append(task.id)
            logger.warning("Payslip generation failed for run employee %s (attempt %s): %s", task.run_employee_id,
                           task.attempts, task.last_error)
            continue
        task.status = "GENERATED"
        task.document_id = doc.id
        task.last_error = None
        generated += 1
    db.flush()
    if retry_ids:
        attempt = max(t.attempts for t in tasks if t.id in retry_ids)
        jobs.enqueue(db, company_id=run.company_id, type=GENERATE_JOB,
                     payload={"run_id": run.id, "task_ids": retry_ids},
                     idempotency_key=f"payslips:{run.id}:retry:{attempt}:{min(retry_ids)}",
                     run_at=clock.utcnow() + jobs.backoff(attempt), read_permission=documents.READ)
    if failed:
        AuditLogService.log(db, None, audit.PAYSLIPS, "PAYSLIP_GENERATION_FAILED",
                            f"{failed} payslip(s) of run {run.number} could not be generated", "payroll_run", run.id,
                            company_id=run.company_id, actor_type="system", details={"failed": failed})
    return {"generated": generated, "failed": failed, "retrying": retrying}


def handle_run_finalized(db: Session, company_id: int, run_id: int) -> dict:
    """The outbox handler: generate the run's payslips, then apply the publication rule (idempotent)."""
    run = _run(db, company_id, run_id)
    if not sm.is_run_finalized(run.status):
        raise jobs.PermanentJobError(f"run {run_id} is not finalized")
    if run.run_type == "REVERSAL":
        return {"reversed": reverse_run_documents(db, run)}
    queued = queue_tasks(db, run)
    result = process_tasks(db, run)
    published = apply_publication(db, company_id, run.id)
    return {"run_id": run.id, "queued": queued, **result, **published}


def retry_failed(db: Session, principal: Principal, run_id: int) -> dict:
    """"Retry failed": re-queue only the FAILED tasks of the run (EC-DOC-04)."""
    run = _staff_run(db, principal, run_id, documents.PUBLISH)
    failed = db.query(DocumentGenerationTask).filter(
        DocumentGenerationTask.company_id == run.company_id, DocumentGenerationTask.run_id == run.id,
        DocumentGenerationTask.status == "FAILED").with_for_update().all()
    for task in failed:
        task.status, task.attempts, task.retries = "QUEUED", 0, task.retries + 1
        task.row_version += 1
    db.flush()
    if failed:
        jobs.enqueue(db, company_id=run.company_id, type=GENERATE_JOB,
                     payload={"run_id": run.id, "task_ids": [t.id for t in failed]},
                     idempotency_key=f"payslips:{run.id}:manual-retry:{uuid.uuid4().hex}",
                     created_by_user_id=principal.user_id, read_permission=documents.READ)
        AuditLogService.log(db, principal, audit.PAYSLIPS, "PAYSLIP_GENERATION_RETRIED",
                            f"Retry of {len(failed)} failed payslip(s) of run {run.number}", "payroll_run", run.id,
                            details={"tasks": [t.id for t in failed]})
    return {"run_id": run.id, "requeued": len(failed)}


def start_generation(db: Session, principal: Principal, run_id: int) -> dict:
    """Queue generation for a finalized run (normally the outbox does it); idempotent per run employee."""
    run = _staff_run(db, principal, run_id, documents.PUBLISH)
    if not sm.is_run_finalized(run.status) or run.run_type == "REVERSAL":
        raise conflict("Payslips are generated for finalized runs only.", "RUN_NOT_FINALIZED")
    queued = queue_tasks(db, run)
    job = jobs.enqueue(db, company_id=run.company_id, type=GENERATE_JOB, payload={"run_id": run.id},
                       idempotency_key=f"payslips:{run.id}:start:{uuid.uuid4().hex}",
                       created_by_user_id=principal.user_id, read_permission=documents.READ)
    return {"run_id": run.id, "queued": queued, "job_id": job.id}


def generation_status(db: Session, principal: Principal, run_id: int) -> dict:
    run = _staff_run(db, principal, run_id, documents.READ)
    tasks = db.query(DocumentGenerationTask).filter(DocumentGenerationTask.company_id == run.company_id,
                                                    DocumentGenerationTask.run_id == run.id).order_by(
        DocumentGenerationTask.id).all()
    counts = {status: 0 for status in ("QUEUED", "GENERATED", "FAILED")}
    for task in tasks:
        counts[task.status] += 1
    rows = {r.id: r.employee_id for r in db.query(PayrollRunEmployee.id, PayrollRunEmployee.employee_id).filter(
        PayrollRunEmployee.company_id == run.company_id, PayrollRunEmployee.run_id == run.id)}
    names = documents.employee_names(db, run.company_id, rows.values())
    failures = [{"task_id": t.id, "run_employee_id": t.run_employee_id, "employee": names.get(rows.get(t.run_employee_id)),
                 "attempts": t.attempts, "retries": t.retries, "error": t.last_error}
                for t in tasks if t.status == "FAILED"]
    return {"run_id": run.id, "total": len(tasks), "counts": counts, "failed": failures,
            "published": db.query(func.count(PayDocument.id)).filter(
                PayDocument.company_id == run.company_id, PayDocument.run_id == run.id,
                PayDocument.doc_type == PAYSLIP, PayDocument.status != "GENERATED").scalar() or 0}


def _staff_run(db: Session, principal: Principal, run_id: int, permission: str) -> PayrollRun:
    from app.payroll.services import access

    documents.require_staff(principal, permission)
    return access.get_in_scope(db, principal, permission, PayrollRun, run_id, "Payroll run")


# --------------------------------------------------------------------------- publication (3.6 §5.1)


def publish_trigger(db: Session, run: PayrollRun, period: PayPeriod) -> tuple[str, Optional[time]]:
    values = payslips.payslip_settings(db, run, period)
    trigger = values.get("payslip.publish_trigger") or "ON_FINALIZE"
    at = values.get("payslip.scheduled_publish_time")
    if isinstance(at, str):
        at = time.fromisoformat(at)
    return str(trigger), at


def _instructions_by_row(db: Session, company_id: int, row_ids: Sequence[int]) -> dict[int, list]:
    from app.models.payment_flows import PaymentInstruction, PaymentObligation

    found: dict[int, list] = {}
    if not row_ids:
        return found
    query = (db.query(PaymentObligation.run_employee_id, PaymentInstruction)
             .join(PaymentInstruction, (PaymentInstruction.company_id == PaymentObligation.company_id)
                   & (PaymentInstruction.obligation_id == PaymentObligation.id))
             .filter(PaymentObligation.company_id == company_id, PaymentObligation.run_employee_id.in_(list(row_ids)),
                     PaymentInstruction.status.notin_(("CANCELLED", "REISSUED"))))
    for run_employee_id, instruction in query:
        found.setdefault(run_employee_id, []).append(instruction)
    return found


def trigger_met(trigger: str, *, net: Decimal, instructions: Sequence[Any], scheduled_at: Optional[datetime],
                now: datetime) -> bool:
    """Whether the publish trigger is met for one payslip (pure; EC-DOC-11)."""
    if trigger == "ON_FINALIZE" or net <= 0:
        return True
    if trigger == "SCHEDULED":
        return scheduled_at is not None and now >= scheduled_at
    if not instructions:
        return False
    if trigger == "ON_PAYMENT_SUBMITTED":
        return any(i.status in IN_FLIGHT_OR_LATER or i.submitted_at is not None for i in instructions)
    if trigger == "ON_SETTLED":
        return all(i.status in FULLY_SETTLED and i.evidence_status == sm.EvidenceStatus.VERIFIED.value
                   for i in instructions)
    return False


def scheduled_moment(entity: LegalEntity, pay_date: date, at: Optional[time]) -> datetime:
    zone = pytz.timezone(entity.timezone or "UTC")
    return zone.localize(datetime.combine(pay_date, at or time(9, 0))).astimezone(pytz.UTC)


def latest_payslips(db: Session, company_id: int, run_id: int) -> list[PayDocument]:
    """The newest version of each payslip of the run."""
    newest = (db.query(PayDocument.source_key, func.max(PayDocument.version).label("v"))
              .filter(PayDocument.company_id == company_id, PayDocument.run_id == run_id,
                      PayDocument.doc_type == PAYSLIP)
              .group_by(PayDocument.source_key).subquery())
    return (db.query(PayDocument)
            .join(newest, (PayDocument.source_key == newest.c.source_key) & (PayDocument.version == newest.c.v))
            .filter(PayDocument.company_id == company_id, PayDocument.run_id == run_id, PayDocument.doc_type == PAYSLIP)
            .order_by(PayDocument.id).all())


def apply_publication(db: Session, company_id: int, run_id: int, *, now: Optional[datetime] = None) -> dict:
    """Publish every generated payslip of the run whose trigger is met; schedule a re-check for the rest."""
    run = _run(db, company_id, run_id)
    if not sm.is_run_finalized(run.status) or run_is_reversed(db, run):
        return {"published": 0, "waiting": 0}
    reversed_rows = reversed_run_employee_ids(db, run)  # a partial reversal blocks only its own employees
    period = _period(db, run)
    trigger, at = publish_trigger(db, run, period)
    entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id,
                                          LegalEntity.id == run.legal_entity_id).one()
    moment = scheduled_moment(entity, period.pay_date, at)
    now = now or clock.utcnow()
    waiting = [d for d in latest_payslips(db, company_id, run.id)
               if d.status == "GENERATED" and not d.draft and d.run_employee_id not in reversed_rows]
    rows = {r.id: r for r in db.query(PayrollRunEmployee).filter(
        PayrollRunEmployee.company_id == company_id, PayrollRunEmployee.id.in_([d.run_employee_id for d in waiting]))}
    instructions = _instructions_by_row(db, company_id, list(rows)) if trigger in (
        "ON_PAYMENT_SUBMITTED", "ON_SETTLED") else {}
    published = 0
    for doc in waiting:
        row = rows.get(doc.run_employee_id)
        net = Decimal(row.net or 0) if row is not None else Decimal(0)
        if not trigger_met(trigger, net=net, instructions=instructions.get(doc.run_employee_id, []),
                           scheduled_at=moment, now=now):
            continue
        documents.transition(db, None, doc, "PUBLISH", trigger=trigger)
        notify(db, doc)
        published += 1
    remaining = len(waiting) - published
    if remaining and now < moment + SWEEP_UNTIL:
        next_at = moment if trigger == "SCHEDULED" and moment > now else now + SWEEP_EVERY
        jobs.enqueue(db, company_id=company_id, type=SWEEP_JOB, payload={"run_id": run.id},
                     idempotency_key=f"sweep:{run.id}:{next_at.strftime('%Y%m%d%H')}", run_at=next_at,
                     read_permission=documents.READ)
    return {"published": published, "waiting": remaining, "trigger": trigger}


def on_payment_progress(db: Session, company_id: int, run_id: int) -> dict:
    """Hook for the payment services: re-check ON_PAYMENT_SUBMITTED / ON_SETTLED payslips of the run now."""
    return apply_publication(db, company_id, run_id)


def publish_run(db: Session, principal: Principal, run_id: int, *, reason: Optional[str] = None) -> dict:
    """HR "Publish": apply the trigger; with a reason, publish the remaining payslips manually (EC-DOC-11)."""
    run = _staff_run(db, principal, run_id, documents.PUBLISH)
    if not sm.is_run_finalized(run.status) or run.run_type == "REVERSAL":
        raise conflict("Payslips are published from finalized runs only.", "RUN_NOT_FINALIZED")
    result = apply_publication(db, run.company_id, run.id)
    manual = 0
    if reason is not None:
        text = reason.strip()
        if not text:
            raise bad_request("A reason is required.", "REASON_REQUIRED")
        if run_is_reversed(db, run):
            raise conflict("The run was reversed; its payslips are not published.", "RUN_REVERSED")
        reversed_rows = reversed_run_employee_ids(db, run)
        for doc in latest_payslips(db, run.company_id, run.id):
            if doc.status == "GENERATED" and not doc.draft and doc.run_employee_id not in reversed_rows:
                documents.transition(db, principal, doc, "PUBLISH_MANUALLY", reason=text)
                notify(db, doc)
                manual += 1
    return {**result, "published_manually": manual, "run_id": run.id}


def publish_manually(db: Session, principal: Principal, document_id: int, reason: str, *,
                     expected_version: Optional[int] = None) -> PayDocument:
    """Manual publication of one payslip with a reason (EC-DOC-11): permission in scope, run not reversed."""
    doc = documents.get_for_staff(db, principal, document_id, doc_types=(PAYSLIP,), permission=documents.PUBLISH)
    run = _run(db, doc.company_id, doc.run_id)
    if doc.run_employee_id in reversed_run_employee_ids(db, run):
        raise conflict("The run was reversed; its payslips are not published.", "RUN_REVERSED")
    latest = documents.latest_version(db, doc.company_id, PAYSLIP, doc.source_key)
    if latest is not None and latest.id != doc.id:
        raise conflict("A newer version of this payslip exists.", "NOT_LATEST_VERSION")
    doc = documents.transition(db, principal, doc, "PUBLISH_MANUALLY", reason=reason,
                               expected_version=expected_version)
    notify(db, doc)
    return doc


def publish_bulk(db: Session, principal: Principal, document_ids: Sequence[int], reason: str) -> dict:
    published, skipped = [], []
    for document_id in dict.fromkeys(document_ids):
        doc = documents.get_for_staff(db, principal, document_id, doc_types=(PAYSLIP,),
                                      permission=documents.PUBLISH)
        if doc.status != "GENERATED":
            skipped.append(doc.id)
            continue
        publish_manually(db, principal, doc.id, reason)
        published.append(doc.id)
    return {"published": published, "skipped": skipped}


# --------------------------------------------------------------------------- notifications (EC-DOC-05, EC-DOC-14)


def _claim_notification(db: Session, doc: PayDocument, channel: str, recipient_user_id: Optional[int]
                        ) -> Optional[DocumentNotification]:
    """The new notification row of (document version, channel), or None when it already exists (EC-DOC-05)."""
    now = clock.utcnow()
    inserted = db.execute(
        insert(DocumentNotification)
        .values(company_id=doc.company_id, document_id=doc.id, channel=channel, recipient_user_id=recipient_user_id,
                status="PENDING", attached=False, created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["company_id", "document_id", "channel"])
        .returning(DocumentNotification.id)
    ).scalar()
    if inserted is None:
        return None
    return db.get(DocumentNotification, inserted)


def notify(db: Session, doc: PayDocument) -> dict:
    """In-app and (when configured) e-mail notification of a published document, once per version and channel."""
    from app.services.notification_service import NotificationService

    if doc.employee_id is None:
        return {}
    employee = db.query(Employee).filter(Employee.company_id == doc.company_id, Employee.id == doc.employee_id).one()
    content = doc.content or {}
    label = (content.get("period") or {}).get("label") or doc.title
    is_payslip = doc.doc_type in (PAYSLIP,)
    result: dict[str, Any] = {}
    in_app = _claim_notification(db, doc, "IN_APP", employee.user_id)
    if in_app is not None:
        if employee.user_id is None:
            in_app.status, in_app.detail = "SKIPPED", "The employee has no login"
        else:
            if is_payslip:
                revised = content.get("title_key") == "REVISED_PAYSLIP"
                title = f"Your {'revised ' if revised else ''}payslip for {label} is ready"
                path = f"/me/payslips/{doc.id}"
            else:
                title = f"New document: {doc.title}"
                path = f"/me/documents/{doc.id}"
            note = NotificationService.create(db, employee.user_id, title,
                                              "Open it in the app to view or download it.", "info", path)
            db.flush()
            in_app.status, in_app.sent_at = "SENT", clock.utcnow()
            in_app.notification_id = note.id if note is not None else None
        result["in_app"] = in_app.status
    if is_payslip and _email_enabled(db, doc):
        email = _claim_notification(db, doc, "EMAIL", employee.user_id)
        if email is not None:
            jobs.enqueue(db, company_id=doc.company_id, type=EMAIL_JOB, payload={"document_id": doc.id},
                         idempotency_key=f"payslip-email:{doc.id}", read_permission=documents.READ)
            result["email"] = "QUEUED"
    db.flush()
    return result


def _settings_for(db: Session, doc: PayDocument) -> dict:
    run = _run(db, doc.company_id, doc.run_id)
    return payslips.payslip_settings(db, run, _period(db, run))


def _email_enabled(db: Session, doc: PayDocument) -> bool:
    if doc.run_id is None:
        return False
    try:
        return _settings_for(db, doc).get("payslip.email_notify") is True
    except Exception:  # noqa: BLE001 - invalid settings: no e-mail (in-app still works)
        logger.exception("payslip.email_notify could not be resolved for document %s", doc.id)
        return False


def employee_secret(db: Session, company_id: int, employee_id: int) -> Optional[str]:
    """The employee's PDF password (decrypted for encryption only; never returned by any endpoint)."""
    from app.payroll.services import keys

    row = db.query(PayslipPdfSecret).filter(PayslipPdfSecret.company_id == company_id,
                                            PayslipPdfSecret.employee_id == employee_id).one_or_none()
    if row is None:
        return None
    return keys.decrypt_field(db, company_id, row.secret_enc, SECRET_PURPOSE, aad=SECRET_AAD + f":{employee_id}".encode())


def set_employee_secret(db: Session, principal: Principal, password: str) -> dict:
    """Set or reset the caller's payslip PDF password (EC-DOC-15): self only, after step-up (route), never shown."""
    from app.payroll.services import keys

    if principal.employee_id is None:
        raise not_found("Employee")
    text = password or ""
    if len(text) < 8 or len(text) > 40 or text.strip() != text:
        raise bad_request("Use 8 to 40 characters without leading or trailing spaces.", "PDF_PASSWORD_INVALID")
    employee = db.query(Employee).filter(Employee.company_id == principal.company_id,
                                         Employee.id == principal.employee_id).one()
    guessable = {employee.employee_code.lower(), (employee.first_name or "").lower(), (employee.last_name or "").lower()}
    if employee.date_of_birth is not None:
        dob = employee.date_of_birth
        guessable |= {dob.strftime(f) for f in ("%d%m%Y", "%Y%m%d", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m%d%Y")}
    if text.lower() in guessable:
        raise bad_request("Choose a password that is not your date of birth, name or employee code.",
                          "PDF_PASSWORD_GUESSABLE")
    sealed = keys.encrypt_field(db, principal.company_id, text, SECRET_PURPOSE,
                                aad=SECRET_AAD + f":{employee.id}".encode())
    row = db.query(PayslipPdfSecret).filter(PayslipPdfSecret.company_id == principal.company_id,
                                            PayslipPdfSecret.employee_id == employee.id).with_for_update().one_or_none()
    now = clock.utcnow()
    if row is None:
        row = PayslipPdfSecret(company_id=principal.company_id, employee_id=employee.id, secret_enc=sealed, set_at=now,
                               row_version=1)
        db.add(row)
        action = "PAYSLIP_PASSWORD_SET"
    else:
        row.secret_enc, row.set_at = sealed, now
        row.row_version += 1
        action = "PAYSLIP_PASSWORD_RESET"
    db.flush()
    AuditLogService.log(db, principal, audit.PAYSLIPS, action, "Payslip PDF password set by the employee",
                        "employee", employee.id, details={"employee_id": employee.id})
    return {"set": True, "set_at": now.isoformat()}


def secret_status(db: Session, principal: Principal) -> dict:
    if principal.employee_id is None:
        raise not_found("Employee")
    row = db.query(PayslipPdfSecret).filter(PayslipPdfSecret.company_id == principal.company_id,
                                            PayslipPdfSecret.employee_id == principal.employee_id).one_or_none()
    return {"set": row is not None, "set_at": row.set_at.isoformat() if row is not None else None}


def _deliver(message, attachment: Optional[tuple[str, bytes]]) -> tuple[bool, str]:
    """Send through SMTP; in development/test without SMTP the message goes to the local outbox folder."""
    import smtplib

    from app.services.email_service import EmailService

    mime = EmailService._mime(message)
    if attachment is not None:
        mime.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    if not EmailService.configured():
        if app_settings.environment in ("development", "test"):
            EmailService._write_outbox(message, mime)
            return True, "Written to the development outbox (SMTP not configured)"
        return False, "Email delivery is not configured"
    try:
        with smtplib.SMTP(app_settings.smtp_host, app_settings.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(app_settings.smtp_username, app_settings.smtp_password)
            server.send_message(mime)
        return True, "Sent"
    except (smtplib.SMTPException, OSError) as exc:
        logger.exception("Payslip e-mail could not be sent")
        return False, f"SMTP error: {type(exc).__name__}"


def send_email(db: Session, company_id: int, document_id: int) -> dict:
    """The e-mail of one payslip version: sent at most once (row lock + status, EC-DOC-05)."""
    from app.services.email_branding import link
    from app.services.email_service import EmailService

    from app.payroll.services import pdf_render

    note = (db.query(DocumentNotification)
            .filter(DocumentNotification.company_id == company_id, DocumentNotification.document_id == document_id,
                    DocumentNotification.channel == "EMAIL").with_for_update().one_or_none())
    if note is None or note.status in ("SENT", "SKIPPED"):
        return {"skipped": True}
    doc = db.query(PayDocument).filter(PayDocument.company_id == company_id, PayDocument.id == document_id).one()
    employee = db.query(Employee).filter(Employee.company_id == company_id, Employee.id == doc.employee_id).one()
    if not employee.email:
        note.status, note.detail = "SKIPPED", "The employee has no e-mail address"
        return {"skipped": True}
    values = _settings_for(db, doc)
    content = doc.content or {}
    context = {
        "recipient": {"first_name": employee.first_name, "name": employee.full_name, "email": employee.email},
        "period": {"label": (content.get("period") or {}).get("label") or doc.title},
        "link": {"url": link(company_id, f"/me/payslips/{doc.id}")},
    }
    message = EmailService.compose(EMAIL_TEMPLATE, employee.email, context, company_id)
    if values.get("payslip.email_include_net_pay") is True:
        totals = content.get("totals") or {}
        net = pdf_render.format_money(totals.get("net", "0"), content.get("currency") or "XXX",
                                      content.get("locale") or "en")
        sentence = f"Net pay: {net}."
        message.text = message.text.replace(PRIVACY_SENTENCE, sentence)
        message.html = message.html.replace(html.escape(PRIVACY_SENTENCE), html.escape(sentence)).replace(
            PRIVACY_SENTENCE, html.escape(sentence))
    attachment, detail = None, None
    if values.get("payslip.email_attach_pdf") is True:
        if values.get("payslip.pdf_protection") != "EMPLOYEE_SECRET":
            detail = "Attachment blocked: PDF protection is off (V-09)"
        else:
            secret = employee_secret(db, company_id, employee.id)
            if secret is None:
                detail = "No attachment: the employee has not set a PDF password"
            else:
                try:
                    data, _ = documents.document_bytes(db, None, doc)
                    attachment = (doc.file_name, pdf_render.protect(data, secret))
                except pdf_render.ProtectionUnavailable:
                    detail = "No attachment: AES-256 PDF protection is unavailable"
    ok, outcome = _deliver(message, attachment)
    now = clock.utcnow()
    note.status = "SENT" if ok else "FAILED"
    note.sent_at = now if ok else None
    note.attached = bool(ok and attachment is not None)
    note.detail = "; ".join(v for v in (outcome, detail) if v)[:300]
    action = "RECORD_EMAIL_SENT" if ok else "RECORD_EMAIL_FAILED"
    if (doc.status, action) in sm.DOCUMENT.transitions:
        documents.transition(db, None, doc, action, details={"attached": note.attached})
    documents.log_access(db, doc, None, "EMAIL", None, {"status": note.status, "attached": note.attached})
    db.flush()
    return {"status": note.status, "attached": note.attached}


def resend(db: Session, principal: Principal, document_id: int) -> dict:
    """Resend a payslip e-mail only when the previous attempt failed (one notification per version, EC-DOC-05)."""
    doc = documents.get_for_staff(db, principal, document_id, doc_types=(PAYSLIP,), permission=documents.PUBLISH)
    if doc.status not in PUBLISHED_STATES:
        raise conflict("Only published payslips are sent.", "NOT_PUBLISHED")
    if not _email_enabled(db, doc):
        raise conflict("Payslip e-mails are switched off (payslip.email_notify).", "EMAIL_DISABLED")
    note = (db.query(DocumentNotification)
            .filter(DocumentNotification.company_id == doc.company_id, DocumentNotification.document_id == doc.id,
                    DocumentNotification.channel == "EMAIL").with_for_update().one_or_none())
    if note is None:
        notify(db, doc)
        attempt = "first"
    elif note.status == "FAILED":
        note.status, note.detail = "PENDING", None
        attempt = uuid.uuid4().hex
        jobs.enqueue(db, company_id=doc.company_id, type=EMAIL_JOB, payload={"document_id": doc.id},
                     idempotency_key=f"payslip-email:{doc.id}:{attempt}", created_by_user_id=principal.user_id,
                     read_permission=documents.READ)
    else:
        raise conflict("This payslip version was already sent; one notification per version.", "ALREADY_NOTIFIED")
    AuditLogService.log(db, principal, audit.PAYSLIPS, "PAYSLIP_EMAIL_RESENT",
                        f"Payslip {doc.number} e-mail queued again", "document", doc.id,
                        details={"document_id": doc.id})
    db.flush()
    return {"document_id": doc.id, "queued": True}


# --------------------------------------------------------------------------- corrections (3.6 §6)


def issue_revised_payslip(db: Session, principal: Optional[Principal], original: PayDocument, *,
                          run: PayrollRun, row: PayrollRunEmployee, reason: str) -> PayDocument:
    """The next version of a payslip, titled "Revised payslip — replaces …"; the original becomes SUPERSEDED."""
    if original.doc_type != PAYSLIP:
        raise ValueError("only payslips are revised")
    latest = documents.latest_version(db, original.company_id, PAYSLIP, original.source_key)
    if latest is None or latest.id != original.id:
        raise conflict("A newer version of this payslip exists.", "NOT_LATEST_VERSION")
    version = original.version + 1
    if original.run_employee_id != row.id:
        raise ValueError("a revised payslip keeps the source key of the original run employee")
    doc = generate_payslip(db, run, row, version=version, title_key="REVISED_PAYSLIP", replaces=original,
                           principal=principal)
    if original.status in PUBLISHED_STATES:
        documents.transition(db, None, original, "SUPERSEDE", details={"replaced_by": doc.id, "reason": reason})
        documents.transition(db, None, doc, "PUBLISH", trigger="ON_ISSUE")
        notify(db, doc)
    return doc


def _reversal_content(original: PayDocument, reversal_run: PayrollRun, row: PayrollRunEmployee) -> dict:
    content = original.content or {}
    currency = content.get("currency") or row.currency
    rows = []
    for section, title in (("earnings", "Earning"), ("deductions", "Deduction"), ("reimbursements", "Reimbursement")):
        for item in content.get(section) or []:
            amount = Decimal(item["amount"])
            rows.append([item.get("label") or item.get("code"), title, amount_str(-amount, currency)])
    totals = content.get("totals") or {}
    net = Decimal(totals.get("net") or "0")
    return {
        "kind": "GENERIC", "title": "Reversal statement", "subtitle": f"Reverses payslip {original.number}",
        "brand": content.get("brand"), "employer": content.get("employer"),
        "languages": content.get("languages") or ["en"], "locale": content.get("locale") or "en",
        "info": [["Employee", (content.get("employee") or {}).get("name")],
                 ["Employee ID", (content.get("employee") or {}).get("code")],
                 ["Pay period", (content.get("period") or {}).get("label")],
                 ["Original payslip", original.number], ["Reversal run", reversal_run.number],
                 ["Reason", reversal_run.status_reason or "Payroll run reversed"]],
        "tables": [{"title": "Reversed amounts", "columns": ["Component", "Type", f"Amount ({currency})"],
                    "numeric": [False, False, True], "widths": [3, 1.2, 1.4], "rows": rows,
                    "total_row": ["Net pay reversed", "", amount_str(-net, currency)]}],
        "notes": ["The original payslip stays available and is marked Reversed."],
        "currency": currency,
        "verify_base": content.get("verify_base"),
    }


def reverse_run_documents(db: Session, reversal_run: PayrollRun, *,
                          run_employee_ids: Optional[Sequence[int]] = None) -> int:
    """EC-DOC-12: the reversed employees' payslips become REVERSED and each gets a reversal statement.

    Only the employees the reversal covers: ``run_employee_ids`` (original run employees, from the
    ``payroll.run.reversed`` event) or else the employees of the finalized REVERSAL run. The original
    documents' content never changes; idempotent (a repeated event issues nothing new).
    """
    if reversal_run.run_type != "REVERSAL" or reversal_run.reverses_run_id is None:
        return 0
    if not sm.is_run_finalized(reversal_run.status):
        return 0
    original_run = _run(db, reversal_run.company_id, reversal_run.reverses_run_id)
    if run_employee_ids is not None:
        wanted_rows = {int(i) for i in run_employee_ids}
        covered = lambda doc: doc.run_employee_id in wanted_rows  # noqa: E731
    else:
        reversed_employees = {e for (e,) in db.query(PayrollRunEmployee.employee_id).filter(
            PayrollRunEmployee.company_id == reversal_run.company_id, PayrollRunEmployee.run_id == reversal_run.id)}
        covered = lambda doc: doc.employee_id in reversed_employees  # noqa: E731
    count = 0
    for doc in latest_payslips(db, original_run.company_id, original_run.id):
        if not covered(doc):
            continue
        row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.company_id == doc.company_id,
                                                  PayrollRunEmployee.id == doc.run_employee_id).one()
        visible = doc.status in PUBLISHED_STATES or doc.status == "SUPERSEDED"
        key = f"reversal:{reversal_run.id}:run_employee:{row.id}"
        statement = documents.find_version(db, doc.company_id, documents.REVERSAL_STATEMENT, key, 1)
        if statement is None:
            statement = documents.issue(
                db, None, company_id=doc.company_id, doc_type=documents.REVERSAL_STATEMENT,
                legal_entity_id=doc.legal_entity_id, pay_group_id=doc.pay_group_id, subject_type="EMPLOYEE",
                subject_id=doc.employee_id, source_key=key, content=_reversal_content(doc, reversal_run, row),
                title=f"Reversal statement — {(doc.content.get('period') or {}).get('label', '')}",
                file_stem=f"reversal-{doc.file_name.rsplit('.', 1)[0]}", issue_date=_today(db, doc.company_id),
                employee_id=doc.employee_id, run_id=reversal_run.id, period=(doc.period_start, doc.period_end)
                if doc.period_start else None, source_refs={"reverses_document_id": doc.id,
                                                          "reversal_run_id": reversal_run.id},
                template_version="REVERSAL_STATEMENT:1", locale=doc.locale, languages=list(doc.languages or ["en"]),
                labels=list(doc.labels or []), publish="ON_ISSUE" if visible else None,
            )
            count += 1
        if visible and doc.status != "REVERSED":
            locked = documents.transition(db, None, doc, "REVERSE", details={"reversal_run_id": reversal_run.id,
                                                                           "statement_id": statement.id})
            locked.reversal_document_id = statement.id
            db.flush()
        if statement.status != "GENERATED":
            notify(db, statement)
    return count


# --------------------------------------------------------------------------- job handlers


@jobs.register(RUN_FINALIZED_JOB)
def _on_run_finalized(ctx: jobs.JobContext) -> dict:
    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("payroll.run.finalized needs a run_id")
    return handle_run_finalized(ctx.db, ctx.company_id, run_id)


@jobs.register(GENERATE_JOB)
def _generate(ctx: jobs.JobContext) -> dict:
    run_id = ctx.payload.get("run_id")
    task_ids = ctx.payload.get("task_ids")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("payslip generation needs a run_id")
    run = _run(ctx.db, ctx.company_id, run_id)
    result = process_tasks(ctx.db, run, task_ids=task_ids if isinstance(task_ids, list) else None)
    return {**result, **apply_publication(ctx.db, ctx.company_id, run.id)}


@jobs.register(SWEEP_JOB)
def _sweep(ctx: jobs.JobContext) -> dict:
    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("the publication sweep needs a run_id")
    return apply_publication(ctx.db, ctx.company_id, run_id)


@jobs.register(EMAIL_JOB)
def _email(ctx: jobs.JobContext) -> dict:
    document_id = ctx.payload.get("document_id")
    if not isinstance(document_id, int):
        raise jobs.PermanentJobError("the payslip e-mail needs a document_id")
    return send_email(ctx.db, ctx.company_id, document_id)


def handle_run_reversed(db: Session, company_id: int, payload: Mapping[str, Any]) -> dict:
    """``payroll.run.reversed`` (corrections): mark the reversed payslips REVERSED, issue reversal statements."""
    reversal_run_id = payload.get("reversal_run_id")
    if not isinstance(reversal_run_id, int):
        raise jobs.PermanentJobError("payroll.run.reversed needs a reversal_run_id")
    reversal_run = _run(db, company_id, reversal_run_id)
    if reversal_run.reverses_run_id != payload.get("run_id"):
        raise jobs.PermanentJobError("the reversal run does not reverse the given run")
    ids = payload.get("run_employee_ids")
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
        raise jobs.PermanentJobError("run_employee_ids must be a list of ids")
    return {"reversed": reverse_run_documents(db, reversal_run, run_employee_ids=ids or None)}


def handle_final_settlement(db: Session, company_id: int, payload: Mapping[str, Any]) -> dict:
    """``payroll.final_settlement.finalized`` (corrections): issue the full-and-final statement, published."""
    from app.payroll.services import registers

    run_id, employee_id = payload.get("run_id"), payload.get("employee_id")
    if not isinstance(run_id, int):
        raise jobs.PermanentJobError("payroll.final_settlement.finalized needs a run_id")
    run = _run(db, company_id, run_id)
    try:
        issued = registers.final_settlement_documents(
            db, None, run, employee_ids=[employee_id] if isinstance(employee_id, int) else None)
    except Exception as exc:  # noqa: BLE001 - a run that is not a finalized settlement never will be
        from app.core.errors import AppError

        if isinstance(exc, AppError) and exc.status_code == 409:
            raise jobs.PermanentJobError(str(exc.detail)) from exc
        raise
    return {"run_id": run.id, "statements": [d.id for d in issued]}


@jobs.register(RUN_REVERSED_JOB)
def _on_run_reversed(ctx: jobs.JobContext) -> dict:
    return handle_run_reversed(ctx.db, ctx.company_id, ctx.payload)


@jobs.register(FINAL_SETTLEMENT_JOB)
def _on_final_settlement(ctx: jobs.JobContext) -> dict:
    return handle_final_settlement(ctx.db, ctx.company_id, ctx.payload)


outbox.subscribe(FINALIZED_TOPIC, RUN_FINALIZED_JOB)
outbox.subscribe(REVERSED_TOPIC, RUN_REVERSED_JOB)
outbox.subscribe(FINAL_SETTLEMENT_TOPIC, FINAL_SETTLEMENT_JOB)


__all__ = [
    "EMAIL_JOB", "FINAL_SETTLEMENT_JOB", "GENERATE_JOB", "MAX_TASK_ATTEMPTS", "RUN_FINALIZED_JOB", "RUN_REVERSED_JOB",
    "SWEEP_JOB", "apply_publication", "employee_secret", "generate_payslip", "generation_status",
    "handle_final_settlement", "handle_run_finalized", "handle_run_reversed", "issue_revised_payslip",
    "latest_payslips", "notify", "on_payment_progress", "process_tasks", "publish_bulk", "publish_manually",
    "publish_run", "queue_tasks", "resend", "retry_failed", "reverse_run_documents", "reversed_run_employee_ids",
    "run_is_reversed",
    "secret_status", "send_email", "set_employee_secret", "start_generation", "trigger_met",
]
