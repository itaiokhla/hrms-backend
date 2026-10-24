"""Payroll notifications (notify.payroll.*, app.payroll.services.notifications; FOLLOW-UPS F-68): daily scheduling per
tenant, recipient scope, privacy, idempotency and tenant isolation of the notification jobs. The ON/OFF cycles of
each key are in tests/payroll/verification/test_settings_onoff.py.

World: ``world`` of test_wage_inputs (Acme: LE01 India with PG-IN, AE01 UAE with PG-AE; the founder (payroll
manager), processor Pia company-wide, entity clerk Cleo for LE01 only, Emma and Gus with self-service logins; Beta
and Cobalt as other tenants).
"""

import uuid
from datetime import date, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytz

from app.payroll.services import notifications as n
from app.services import email_templates
from tests.payroll.test_bank_accounts import bank_world, in_account, step_up, submit  # noqa: F401  (fixture)
from tests.payroll.test_runs_lifecycle import calendar_for, new_run, periods_of
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    role,
    session,
    set_setting,
    sql,
    today_of,
    user_with_role,
    world,
)

CUTOFF_JOB, DIGEST_JOB = "payroll.input_cutoff_reminders", "payroll.exceptions_digest"
REMINDER, DIGEST = "Payroll inputs close on%", "Payroll exceptions:%"


def notes_of(user_id, title_like="%"):
    return [{"title": t, "message": m, "url": u} for t, m, u in sql(
        "SELECT title, message, action_url FROM notifications WHERE user_id = :u AND title LIKE :t ORDER BY id",
        u=user_id, t=title_like)]


def email_jobs(company_id, kind):
    return sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type = 'payroll.notify_email' AND "
               "payload->>'kind' = :k", c=company_id, k=kind)[0][0]


def run_daily(company_id, job_type, day):
    """The daily job for ``day``, due now under its own key (inline mode runs it and what it queues)."""
    from app.payroll.services import jobs

    with session(company_id) as (db, _):
        job_id = jobs.enqueue(db, company_id=company_id, type=job_type, payload={"run_date": day.isoformat()},
                              idempotency_key=f"test:{day.isoformat()}:{uuid.uuid4().hex[:8]}").id
        db.commit()
    assert sql("SELECT status FROM jobs WHERE id = :j", j=job_id)[0][0] == "SUCCEEDED"
    assert sql("SELECT count(*) FROM jobs WHERE company_id = :c AND type LIKE 'payroll.notify%' AND "
               "status <> 'SUCCEEDED'", c=company_id)[0][0] == 0


def next_period(client, group_id):
    """The first period whose input cutoff is more than three days away."""
    limit = (today_of(client.company_id) + timedelta(days=3)).isoformat()
    return next(p for p in periods_of(client, group_id) if p["input_cutoff"] > limit)


def employee(world, name):
    first, last, address = sql("SELECT first_name, last_name, email FROM employees WHERE id = :e",
                               e=world["emp"][name])[0]
    return {"id": world["emp"][name], "first_name": first, "last_name": last, "email": address}


def test_daily_notification_jobs_are_queued_per_tenant_at_eight_company_time(world):
    """A pay calendar starts the tenant's daily maintenance; the cutoff reminder and digest jobs join it (one per
    tenant and date, at 08:00 company time), idempotently, and no other tenant gets any."""
    from app.payroll.services import notifications

    founder, beta = world["u"]["founder"], world["u"]["beta"]
    calendar_for(founder, world["groups"]["IN"])
    rows = sql("SELECT type, idempotency_key, run_at, payload->>'run_date' FROM jobs WHERE company_id = :c AND "
               "type IN (:a, :b) ORDER BY type", c=world["cid"], a=CUTOFF_JOB, b=DIGEST_JOB)
    assert [r[0] for r in rows] == [DIGEST_JOB, CUTOFF_JOB], rows
    zone = pytz.timezone(sql("SELECT coalesce(timezone, 'UTC') FROM companies WHERE id = :c", c=world["cid"])[0][0])
    for job_type, key, run_at, run_date in rows:
        prefix = "cutoff-reminders" if job_type == CUTOFF_JOB else "exceptions-digest"
        assert key == f"{prefix}:{run_date}"
        local = run_at.astimezone(zone)
        assert (local.date().isoformat(), local.time()) == (run_date, time(8, 0)), (run_at, zone)
    with session(world["cid"]) as (db, _):
        notifications.schedule_daily(db, world["cid"], on=date.fromisoformat(rows[0][3]))
        db.commit()
    count = "SELECT count(*) FROM jobs WHERE company_id = :c AND type IN (:a, :b)"
    assert sql(count, c=world["cid"], a=CUTOFF_JOB, b=DIGEST_JOB)[0][0] == 2
    assert sql(count, c=beta.company_id, a=CUTOFF_JOB, b=DIGEST_JOB)[0][0] == 0


def test_cutoff_reminders_reach_only_staff_whose_scope_covers_the_pay_group(world):
    """Cleo holds payroll:inputs:edit for LE01 only: she is reminded of PG-IN, never of PG-AE. Pia and the founder
    (company-wide) are reminded of both; Emma and Gus (self-service) and the other tenants of neither."""
    founder, pia, cleo = world["u"]["founder"], world["u"]["pia"], world["u"]["cleo"]
    calendar_for(founder, world["groups"]["IN"])
    calendar_for(founder, world["groups"]["AE"])
    p_in = next_period(founder, world["groups"]["IN"])
    p_ae = next(p for p in periods_of(founder, world["groups"]["AE"]) if p["start"] == p_in["start"])
    cut_in, cut_ae = date.fromisoformat(p_in["input_cutoff"]), date.fromisoformat(p_ae["input_cutoff"])
    day = cut_in - timedelta(days=2)
    run_daily(world["cid"], CUTOFF_JOB, day)
    ae_due = cut_ae - timedelta(days=2) <= day <= cut_ae
    assert [n["message"].split(" (")[0] for n in notes_of(cleo.user_id, REMINDER)] == [
        "Inputs for pay group Group PG-IN"]
    assert len(notes_of(pia.user_id, REMINDER)) == len(notes_of(founder.user_id, REMINDER)) == (2 if ae_due else 1)
    if not ae_due:  # the UAE calendar moved its cutoff: its own window
        run_daily(world["cid"], CUTOFF_JOB, cut_ae)
        assert len(notes_of(pia.user_id, REMINDER)) == len(notes_of(founder.user_id, REMINDER)) == 2
    assert len(notes_of(cleo.user_id, REMINDER)) == 1                         # never PG-AE
    for name in ("emma", "gus"):
        assert notes_of(world["u"][name].user_id, REMINDER) == []
    for other in ("beta", "cobalt"):
        assert sql("SELECT count(*) FROM notifications WHERE company_id = :c AND title LIKE :t",
                   c=world["u"][other].company_id, t=REMINDER)[0][0] == 0


def _finding(cid, run_id, code, severity, message):
    sql("INSERT INTO payroll_exceptions (company_id, run_id, code, severity, source, message, details, resolutions, "
        "waivable, blocks_finalization, dedupe_key, status, row_version, created_at, updated_at) VALUES (:c, :r, "
        ":code, :s, 'VALIDATION', :m, '{}', '[]', false, false, :code, 'OPEN', 1, now(), now())",
        c=cid, r=run_id, code=code, s=severity, m=message)


def test_exceptions_digest_counts_only_runs_in_the_recipients_scope_and_never_texts_or_amounts(app, world):
    """Two draft runs with open findings whose messages carry amounts and names. Ivan may create runs for LE01 only:
    his digest counts the PG-IN run alone; Pia's (company-wide) both. No digest repeats an exception text."""
    founder, pia = world["u"]["founder"], world["u"]["pia"]
    runs = {}
    for code in ("IN", "AE"):
        calendar_for(founder, world["groups"][code])
        runs[code] = new_run(founder, world["groups"][code], next_period(founder, world["groups"][code])["id"])
    _finding(world["cid"], runs["IN"]["id"], "NEGATIVE_NET", "BLOCKING", "Emma Test: net pay -98,765.43 INR")
    _finding(world["cid"], runs["IN"]["id"], "NET_DROP", "WARNING", "Net pay of Ivan fell by 12,345.67 INR")
    _finding(world["cid"], runs["AE"]["id"], "MISSING_BANK", "WARNING", "Gus Test has no bank account")
    runner = role(founder, "Entity runner", "LEGAL_ENTITY", ("payroll:run:read", "payroll:run:create"))
    ivan = user_with_role(app, founder, employee(world, "Ivan"), runner, [world["le01"]["id"]])
    run_daily(world["cid"], DIGEST_JOB, today_of(world["cid"]) + timedelta(days=1))
    [mine] = notes_of(ivan.user_id, DIGEST)
    assert mine["title"] == "Payroll exceptions: 2 open" and "(1 blocking) in 1 payroll run." in mine["message"]
    assert runs["IN"]["number"] in mine["message"] and runs["AE"]["number"] not in mine["message"]
    assert mine["url"] == f"/payroll/runs/{runs['IN']['id']}"
    [wide] = notes_of(pia.user_id, DIGEST)
    assert wide["title"] == "Payroll exceptions: 3 open" and "in 2 payroll runs." in wide["message"]
    assert wide["url"] == "/payroll/runs"
    for note in (mine, wide):
        for leaked in ("98,765", "98765", "12,345", "Emma", "Gus", "bank account"):
            assert leaked not in note["message"], (leaked, note)
    assert notes_of(world["u"]["cleo"].user_id, DIGEST) == []                 # no payroll:run:create
    assert notes_of(world["u"]["emma"].user_id, DIGEST) == []
    assert email_jobs(world["cid"], "EXCEPTIONS_DIGEST") == len({ivan.user_id, pia.user_id, founder.user_id})


def test_bank_change_approvals_keep_the_in_app_message_when_payroll_channels_are_empty(bank_world):
    """notify.payroll.approval_requested covers runs, revisions, inputs and payments. A bank change (which has its
    own locked alerts) still tells its approvers in the app when the payroll channels are empty, and never by the
    payroll approval e-mail."""
    w = bank_world
    set_setting(w["cid"], "notify.payroll.approval_requested", [])
    emma, ava = w["u"]["emma"], w["u"]["ava"]
    step_up(emma)
    change = submit(emma, in_account())
    assert change["status"] == "PENDING_APPROVAL", change
    titles = [n["title"] for n in notes_of(ava.user_id, "%awaiting your approval")]
    assert titles == ["Bank details change awaiting your approval"], titles
    assert email_jobs(w["cid"], "APPROVAL_REQUESTED") == 0


def test_payment_credited_is_sent_once_per_instruction_even_when_the_hook_runs_again(app):
    """on_settled can run again for an instruction (SETTLED -> RECONCILED, a manual settlement after evidence): the
    event key credited:<instruction> keeps it to one message and one e-mail (regression: no duplicates)."""
    from app.payroll.services import notifications
    from app.payroll.services import payment_events as events
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_payment_events import evidence, sandbox_world
    from tests.payroll.test_payment_obligations import instructions_of, payments_world

    pay = payments_world(app, "Credited Once", employees=("Emma",))
    user_id = login_as(app, pay["admin"], pay["emp"]["Emma"], ["employee"]).user_id
    run_id, _ = sandbox_world(pay, {"Emma": "100.00"})
    iid = instructions_of(run_id)[pay["emp"]["Emma"]["id"]]["id"]
    evidence(pay["cid"], iid, "SETTLED", source="WEBHOOK", event_id="once-1", sandbox=True)
    for _ in range(2):
        with session(pay["cid"]) as (db, _principal):
            row = events.load_instruction(db, pay["cid"], iid)
            assert notifications.payment_credited(db, row) is True        # queued again; the key deduplicates
            db.commit()
    assert [n["title"] for n in notes_of(user_id, "Salary credited")] == ["Salary credited"]
    assert email_jobs(pay["cid"], "PAYMENT_CREDITED") == 1


def test_notification_jobs_never_reach_another_tenants_records(world):
    """Tenant isolation of the new job handlers: Acme jobs naming a Beta period (a forged payload) find nothing
    (company filter + RLS) and notify nobody in either tenant; an unknown kind fails permanently."""
    from app.payroll.services import jobs

    founder, beta = world["u"]["founder"], world["u"]["beta"]
    beta_group = sql("INSERT INTO pay_groups (company_id, legal_entity_id, code, name, currency, pay_frequency, "
                     "calendar_rules, status, created_at, updated_at) SELECT :c, id, 'PG-B', 'Beta group', 'INR', "
                     "'MONTHLY', '{\"monthly_start_day\": 1}'::jsonb, 'ACTIVE', now(), now() FROM legal_entities "
                     "WHERE company_id = :c ORDER BY id LIMIT 1 RETURNING id", c=beta.company_id)[0][0]
    calendar_for(beta, beta_group)
    beta_period = periods_of(beta, beta_group)[0]["id"]
    before = sql("SELECT count(*) FROM notifications")[0][0]
    forged = (("payroll.notify", {"kind": "INPUT_CUTOFF", "period_id": beta_period}),
              ("payroll.notify_email", {"kind": "INPUT_CUTOFF", "period_id": beta_period, "user_id": founder.user_id}),
              ("payroll.notify", {"kind": "NOT_A_KIND"}))
    with session(world["cid"]) as (db, _):
        ids = [jobs.enqueue(db, company_id=world["cid"], type=job_type, payload=payload,
                            idempotency_key=f"forged-{i}").id for i, (job_type, payload) in enumerate(forged)]
        db.commit()
    results = [tuple(r) for r in sql("SELECT status, result FROM jobs WHERE id = ANY(:i) ORDER BY id", i=ids)]
    assert results[0] == ("SUCCEEDED", {"sent": 0}), results
    assert results[1][0] == "SUCCEEDED" and results[1][1]["skipped"] is True, results
    assert results[2][0] == "FAILED", results
    assert sql("SELECT count(*) FROM notifications")[0][0] == before


# ---------------------------------------------------------------------------
# Texts and templates (no database): no amount, account number or exception text can leak
# ---------------------------------------------------------------------------
PAYROLL_TEMPLATES = ("payroll.approval_requested", "payroll.input_cutoff_reminder", "payroll.payment_status",
                     "payroll.exceptions_digest")
MONEY_WORDS = ("amount", "net", "gross", "salary", "pay_amount", "account_number", "iban", "total")


@pytest.mark.parametrize("key", PAYROLL_TEMPLATES)
def test_templates_exist_render_and_offer_no_money_or_bank_placeholder(key):
    template = email_templates.TEMPLATES[key]
    for placeholder in template.placeholders:
        leaf = placeholder.split(".")[-1]
        assert leaf not in MONEY_WORDS, placeholder
    rendered = email_templates.render(key, email_templates.sample_context(key, "Acme", "HRMS"),
                                      {"display_name": "Acme", "product_name": "HRMS"})
    assert rendered.subject and not rendered.used_fallback
    assert "amount" in (template.detail or "").lower() or "amounts" in rendered.text.lower()


def test_every_notification_kind_has_an_email_template():
    assert set(n.TEMPLATES.values()) == set(PAYROLL_TEMPLATES)
    assert set(n.TEMPLATES) == {n.APPROVAL, n.CUTOFF, n.DIGEST, n.CREDITED}


def test_credited_text_names_the_masked_account_and_never_the_amount():
    row = SimpleNamespace(beneficiary_snapshot={"account_masked": "••••4567"}, reference="PAY-000123",
                          amount=Decimal("1234.56"), account_amount=Decimal("1234.56"))
    title, message = n._credited_text(row)
    assert title == "Salary credited"
    assert "••••4567" in message and "PAY-000123" in message
    assert "1234" not in message and "1,234" not in message
    bare = SimpleNamespace(beneficiary_snapshot={}, reference=None, amount=Decimal("5"))
    assert n._credited_text(bare)[1].startswith("Your payment was credited to your bank account.")


def test_digest_text_counts_only_and_caps_the_run_list():
    items = [{"run_id": i, "label": f"PR-{i:04d} Monthly · Group", "open": 2, "blocking": 1} for i in range(1, 13)]
    title, summary, lines, path = n._digest_text(items)
    assert title == "Payroll exceptions: 24 open"
    assert summary == "24 open exceptions (12 blocking) in 12 payroll runs."
    assert lines.count("\n") == n.DIGEST_MAX_RUNS and lines.endswith("and 2 more runs")
    assert path == "/payroll/runs"
    assert n._digest_text(items[:1])[3] == "/payroll/runs/1"
    assert n._digest_text(items[:1])[1] == "2 open exceptions (1 blocking) in 1 payroll run."


def test_cutoff_text_for_staff_and_managers():
    period = SimpleNamespace(period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
                             input_cutoff_on=date(2026, 11, 25))
    group = SimpleNamespace(name="Monthly staff")
    title, staff, staff_path = n._cutoff_text(period, group, "STAFF")
    _, team, team_path = n._cutoff_text(period, group, "MANAGER")
    assert title == "Payroll inputs close on 25 Nov 2026"
    assert staff.startswith("Inputs for pay group Monthly staff (01 Nov – 30 Nov 2026) close on 25 Nov 2026.")
    assert team.startswith("Payroll inputs for your team in Monthly staff")
    assert (staff_path, team_path) == ("/payroll/inputs", "/payroll/approvals")
    assert n._continued(staff).startswith("inputs for pay group")


@pytest.mark.parametrize("module", sorted(n.UNCOVERED_APPROVAL_MODULES))
def test_bank_and_settings_approvals_keep_in_app_without_reading_the_setting(module):
    request = SimpleNamespace(module=module, company_id=1, context={})
    assert n.approval_channels(None, request) == ("IN_APP",)       # no database access: db is None


def test_the_notify_keys_are_the_registered_ones():
    from app.payroll import settings_registry as reg

    for key in (n.APPROVAL_KEY, n.CUTOFF_KEY, n.PAYMENT_STATUS_KEY, n.BANK_ALERT_KEY, n.DIGEST_KEY):
        assert reg.definition(key).section == reg.S_NOTIFICATIONS
    assert reg.definition(n.CUTOFF_KEY).constraints["max"] == n.MAX_CUTOFF_DAYS
    assert reg.definition(n.BANK_ALERT_KEY).constraints["const"] is True
