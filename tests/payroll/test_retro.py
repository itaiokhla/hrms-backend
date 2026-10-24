"""Retro deltas: arrears proposals for finalized periods, approval, instalments, payment through the next
regular run or a RETRO_ADJUSTMENT run, the corrected view, the settings and compensation hooks and the
isolation of the corrections tables (3.3 §8.3, 3.4 §16, 3.5 §12; settings 3.1 §2.3).

EC-CMP-12, EC-COR-01, EC-COR-02, EC-COR-05, EC-COR-17, EC-ATT-06, EC-LV-03, EC-LV-04, EC-CFG-03, EC-VAR-01,
EC-SEC-01, EC-SEC-05, EC-RUN-17.

The run helpers here (``calculate``, ``finalize``, ``pay``) stand in for the calculation and finalization
services of the run milestone, which were not on this branch when the corrections were built: they freeze
each included employee's inputs from the live sources (compensation segments, the day ledger inputs, the
catalogue and the arrears hook), calculate with the real engine, store snapshots, lines and figures, move the
run through ``runs.transition`` and emit ``payroll.run.finalized`` like finalization does. The expected
amounts in the tests are computed by hand from the structure (CTC 12,00,000: BASIC 50,000, HRA 20,000,
FIXED_ALLOWANCE 30,000 a month), never by the engine.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    new_run,
    periods_of,
    principal_of,
    runs_world,
)
from tests.payroll.test_wage_inputs import DENIED, audit_actions, sql, tenant

ARREARS = f"{PAY}/arrears-proposals"
APPROVALS = f"{PAY}/approvals"
SNAPSHOT_SETTINGS = {
    "payroll.lop.proration_basis": "CALENDAR_DAYS",
    "payroll.attendance.enabled": False,
    "payroll.leave.enabled": False,
    "payroll.deductions.min_net_pay_percent": "50",
    "payroll.arrears.recovery_mode": "NEXT_RUN",
}
CORRECTION_TABLES = ("arrears_proposals", "arrears_proposal_lines", "arrears_instalments", "payroll_reversals",
                     "final_settlement_cases", "final_settlement_items", "final_settlement_deadlines")


# ---------------------------------------------------------------------------
# Run helpers (stand-in for the calculation and finalization services)
# ---------------------------------------------------------------------------
def snapshot_for(db, run, row, *, settings=None, policies=None, extra=None):
    """The frozen engine snapshot of one run employee, built from the live sources."""
    from app.models.employee import Employee
    from app.models.org import LegalEntity
    from app.models.pay_runs import PayPeriod
    from app.payroll.engine import snapshot as snap
    from app.payroll.services import compensation, retro, time_inputs
    from app.payroll.services import components as comp

    employee = db.get(Employee, row.employee_id)
    period = db.query(PayPeriod).filter(PayPeriod.id == run.pay_period_id).one()
    entity = db.query(LegalEntity).filter(LegalEntity.id == run.legal_entity_id).one()
    pieces = compensation.segments(db, run.company_id, row.employee_id, period.period_start, period.period_end)
    segments = [{"key": f"S{i + 1}", "start": p["start"].isoformat(), "end": p["end"].isoformat(),
                 "assignment_id": p["assignment_id"], "compensation": p["compensation"]}
                for i, p in enumerate(pieces)]
    days = []
    for day in time_inputs.ledger_inputs(db, run.company_id, row.employee_id, period.period_start, period.period_end):
        item = snap.day_to_dict(day)
        item["segment"] = next(s["key"] for s in segments if s["start"] <= item["date"] <= s["end"])
        days.append(item)
    catalogue = comp.catalogue(db, run.company_id, period.period_end)
    data = {
        "run": {"run_id": run.id, "run_employee_id": row.id, "run_type": run.run_type, "environment": "test"},
        "employee": {"employee_id": employee.id, "code": employee.employee_code,
                     "hire_date": employee.hire_date.isoformat(), "country": entity.country_code,
                     "exit_date": employee.termination_date.isoformat() if employee.termination_date else None},
        "period": {"period_id": period.id, "start": period.period_start.isoformat(),
                   "end": period.period_end.isoformat(), "pay_date": period.pay_date.isoformat(),
                   "number_in_tax_year": period.number_in_tax_year},
        "currency": run.currency,
        "settings": {**SNAPSHOT_SETTINGS, **(settings or {})},
        "policies": policies or {},
        "components": [comp.component_def(entry) for entry in catalogue.values()],
        "segments": segments,
        "days": days,
        "arrears": retro.arrears_for_snapshot(db, run, row),
        **(extra or {}),
    }
    return snap.Snapshot.from_dict(data).to_dict()


def _store(db, run, row, data, result):
    from app.core import clock
    from app.models.compensation import PayComponent
    from app.models.pay_runs import PayrollInputSnapshot, PayrollLine
    from app.payroll.engine import snapshot as snap

    version = (db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.run_employee_id == row.id).count()) + 1
    frozen = PayrollInputSnapshot(company_id=run.company_id, run_employee_id=row.id, version=version, content=data,
                                  content_hash=snap.content_hash(data), source_hashes={})
    db.add(frozen)
    db.flush()
    ids = {c: i for c, i in db.query(PayComponent.code, PayComponent.id).filter(
        PayComponent.company_id == run.company_id)}
    for index, line in enumerate(result.lines):
        db.add(PayrollLine(
            company_id=run.company_id, run_id=run.id, run_employee_id=row.id, component_code=line.component,
            component_id=ids.get(line.component), component_version_id=line.component_version_id, kind=line.kind,
            category=line.category, stage=line.stage, amount=line.amount, currency=line.currency, units=line.units,
            rate=line.rate, period_ref={"ref": line.period_ref} if line.period_ref else None,
            segment={"index": line.segment} if line.segment is not None else None, tags=list(line.tags),
            visibility=line.visibility, sources=snap.to_plain(list(line.sources)), trace=snap.to_plain(line.trace),
            explanation=line.explanation, sort_order=line.sort_order or index, statutory=line.statutory,
            fixture=line.fixture))
    totals = result.totals
    row.snapshot_id, row.status = frozen.id, "CALCULATED"
    row.gross, row.deductions, row.reimbursements = totals.gross, totals.deductions, totals.reimbursements
    row.net, row.employer_cost = totals.net, totals.employer_cost
    row.content_hash, row.calc_hash, row.calculated_at = result.content_hash, result.calc_hash, clock.utcnow()
    row.row_version += 1
    db.flush()


def calculate(client, run_id, *, settings=None, policies=None, extra=None):
    """DRAFT -> REVIEW_REQUIRED with engine results for every included employee."""
    from app.models.pay_runs import PayrollRunEmployee
    from app.payroll import engine
    from app.payroll.services import runs

    db, principal = principal_of(client)
    try:
        run = runs.load_run(db, principal.company_id, run_id)
        run = runs.transition(db, principal, run, "PRECHECK_PASSED")
        run = runs.transition(db, principal, run, "CALCULATE", IDEMPOTENCY_KEY=True)
        rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id,
                                                   PayrollRunEmployee.status == "PENDING").all()
        for row in rows:
            data = snapshot_for(db, run, row, settings=settings, policies=policies, extra=extra)
            _store(db, run, row, data, engine.calculate(data))
        runs.transition(db, None, run, "COMPLETE_CALCULATION", system=True)
        runs.transition(db, None, run, "PASS_VALIDATION", system=True, NO_BLOCKING_CHECK_FAILED=True)
        db.commit()
    finally:
        db.close()


def finalize(w, run_id, *, approver=None):
    """REVIEW_REQUIRED -> FINALIZED and the outbox event payroll.run.finalized (as finalization does)."""
    from app.payroll.services import outbox, runs

    db, manager = principal_of(w["acme"])
    adb, checker = principal_of(approver or w["approver"])
    try:
        run = runs.load_run(db, manager.company_id, run_id)
        runs.transition(db, manager, run, "SUBMIT_FOR_APPROVAL", VARIANCE_FLAGS_ACKNOWLEDGED=True)
        db.commit()
        run = runs.load_run(adb, checker.company_id, run_id)
        runs.transition(adb, checker, run, "APPROVE_FINAL", SEPARATION_OF_DUTIES=True, NO_SELF_PAY_APPROVAL=True,
                        LAST_STEP=True)
        adb.commit()
        run = runs.load_run(db, manager.company_id, run_id)
        runs.transition(db, manager, run, "FINALIZE", STEP_UP_MFA=True, STATUTORY_PACK_VERIFIED=True,
                        IDEMPOTENCY_KEY=True)
        outbox.emit(db, "payroll.run.finalized", {"run_id": run_id})
        db.commit()
    finally:
        db.close()
        adb.close()


def pay(w, run_id, **kw):
    calculate(w["acme"], run_id, **kw)
    finalize(w, run_id)


def paid_regular_run(w, period=None, **kw):
    """A finalized regular run of the pay group for the period (the current one by default)."""
    period = period or w["period"]
    run = new_run(w["acme"], w["group"], period["id"])
    pay(w, run["id"], **kw)
    return run


def next_period(w, period=None):
    period = period or w["period"]
    return next(p for p in periods_of(w["acme"], w["group"]) if p["start"] > period["end"])


def run_employee_id(run_id, employee_id):
    return sql("SELECT id FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e AND status = 'CALCULATED'",
               r=run_id, e=employee_id)[0][0]


def lines_of(run_employee):
    return {code: Decimal(amount) for code, amount in sql(
        "SELECT component_code, sum(amount) FROM payroll_lines WHERE run_employee_id = :r GROUP BY component_code",
        r=run_employee)}


def detect(client, employee_id, trigger, from_date, expect=200, **extra):
    body = {"employee_id": employee_id, "trigger": trigger, "from_date": str(from_date),
            "reason": "Correction after payroll", **extra}
    r = client.post(f"{ARREARS}/detect", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def proposal(client, proposal_id, expect=200):
    r = client.get(f"{ARREARS}/{proposal_id}")
    assert r.status_code == expect, r.text
    return r.json()


def deltas(data):
    return {line["component_code"]: Decimal(line["delta"]["amount"]) for line in data["lines"]}


def lop(client, employee_id, day):
    r = client.post(f"{PAY}/lop-inputs", json={"employee_id": employee_id, "date": str(day), "portion": "FULL",
                                               "reason": "Unauthorised absence"})
    assert r.status_code == 201, r.text
    return r.json()


def approve_request(client, request_id, expect=200):
    r = client.post(f"{APPROVALS}/{request_id}/approve", json={})
    assert r.status_code == expect, r.text
    return r


def set_tenant_setting(w, key, value, reason="Corrections test"):
    """A TENANT value from today (written like a configuration profile row, without change control)."""
    from app.core.clock import company_today
    from app.payroll import settings_registry as reg
    from app.payroll.services import settings as settings_engine
    from tests.payroll.test_runs_lifecycle import session_for

    db = session_for(w["cid"])
    try:
        settings_engine.materialize_row(db, w["cid"], key=key, value=reg.to_json(key, reg.validate_value(key, value)),
                                        locked=False, effective_from=company_today(db, w["cid"]), source="ADMIN",
                                        profile_version="TEST", reason=reason)
        db.commit()
    finally:
        db.close()


def working_day(period, offset=5):
    """A Tuesday of the period (never a weekly off or the period edge)."""
    day = date.fromisoformat(period["start"]) + timedelta(days=offset)
    while day.weekday() != 1:
        day += timedelta(days=1)
    return day


@pytest.fixture
def retro_world(app, runs_world):
    """runs_world with HR (approves LOP inputs) and a finalized October-like current period for Emma and Ivan."""
    w = runs_world
    w["hr"] = login_as(app, w["acme"], create_employee(w["acme"], "Hana", "HR"), ["hr_admin"])
    w["run"] = paid_regular_run(w)
    w["emma"] = w["emp"]["Emma"]["id"]
    w["ivan"] = w["emp"]["Ivan"]["id"]
    w["emma_row"] = run_employee_id(w["run"]["id"], w["emma"])
    return w


# ---------------------------------------------------------------------------
# Backdated salary (EC-CMP-12, EC-COR-01)
# ---------------------------------------------------------------------------
def test_ec_cmp_12_ec_cor_01_backdated_salary_proposes_arrears_and_never_touches_the_finalized_run(retro_world):
    w = retro_world
    run_id, emma, row = w["run"]["id"], w["emma"], w["emma_row"]
    before = lines_of(row)
    assert before["BASIC"] == Decimal("50000.00") and before["HRA"] == Decimal("20000.00")
    state = sql("SELECT status, row_version FROM payroll_runs WHERE id = :r", r=run_id)[0]
    assign_and_approve(w["acme"], w["approver"], emma, w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    # Approving the backdated salary proposed the arrears by itself (the compensation hook).
    [(auto_id, auto_status, auto_trigger)] = sql(
        "SELECT id, status, trigger FROM arrears_proposals WHERE original_run_employee_id = :r", r=row)
    assert (auto_status, auto_trigger) == ("PENDING_APPROVAL", "COMPENSATION")
    result = detect(w["acme"], emma, "COMPENSATION", w["period"]["start"])
    [item] = result["items"]
    assert item["run_employee_id"] == row and item["status"] == "PENDING_APPROVAL"
    assert item["superseded_id"] == auto_id  # recalculating again replaces the pending proposal
    data = proposal(w["acme"], item["proposal_id"])
    # By hand: CTC 15,00,000 -> 1,25,000 a month: BASIC 62,500 (+12,500), HRA 25,000 (+5,000), FA 37,500 (+7,500).
    assert deltas(data) == {"BASIC": Decimal("12500.00"), "HRA": Decimal("5000.00"),
                            "FIXED_ALLOWANCE": Decimal("7500.00")}
    assert {l["direction"] for l in data["lines"]} == {"ARREARS"}
    assert data["totals"]["net_change"] == {"amount": "25000.00", "currency": "INR"}
    assert data["original_period"]["id"] == w["period"]["id"] and data["trigger"] == "COMPENSATION"
    basic = next(l for l in data["lines"] if l["component_code"] == "BASIC")
    assert "LOP_APPLICABLE" in basic["tags"] and basic["period_ref"]["run_id"] == run_id  # tags are inherited
    # EC-RUN-17: the finalized run and its lines are untouched.
    assert lines_of(row) == before
    assert sql("SELECT status, row_version FROM payroll_runs WHERE id = :r", r=run_id)[0] == state
    assert "ARREARS_PROPOSED" in audit_actions(w["cid"], "arrears_proposal", item["proposal_id"])


def test_ec_cmp_12_the_compensation_hook_proposes_for_each_finalized_period(retro_world):
    from app.models.compensation import CompensationAssignment
    from app.payroll.services import compensation, retro

    w = retro_world
    assigned = assign_and_approve(w["acme"], w["approver"], w["ivan"], w["version"], "1800000",
                                  effective_from=date.fromisoformat(w["period"]["start"]))
    db, approver = principal_of(w["approver"])
    try:
        row = db.query(CompensationAssignment).filter(CompensationAssignment.id == assigned["id"]).one()
        periods = retro.finalized_periods(db, w["cid"], w["ivan"], row.effective_from)
        assert [p["run_id"] for p in periods] == [w["run"]["id"]]
        created = compensation.arrears_proposals(db, approver, row, periods)
        db.commit()
    finally:
        db.close()
    [entry] = created
    data = proposal(w["acme"], entry["proposal_id"])
    # 18,00,000 -> 1,50,000: BASIC 75,000 (+25,000), HRA 30,000 (+10,000), FA 45,000 (+15,000).
    assert deltas(data) == {"BASIC": Decimal("25000.00"), "HRA": Decimal("10000.00"),
                            "FIXED_ALLOWANCE": Decimal("15000.00")}
    # The approver of the salary change raised the proposal: they cannot approve it (maker-checker).
    r = w["approver"].post(f"{ARREARS}/{entry['proposal_id']}/approve", json={})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Attendance correction -> recovery paid in the next regular run (EC-COR-02, EC-ATT-06, EC-COR-17, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_cor_02_ec_att_06_lop_after_finalization_is_recovered_in_the_next_regular_run(retro_world):
    """EC-COR-17: an overpayment found after finalization is recovered in the next run within the protected net."""
    w = retro_world
    set_tenant_setting(w, "payroll.deductions.min_net_pay_percent", "50")
    set_tenant_setting(w, "payroll.arrears.recovery_mode", "NEXT_RUN")
    emma, row = w["emma"], w["emma_row"]
    day = working_day(w["period"])
    entry = lop(w["acme"], emma, day)
    approve_request(w["hr"], entry["approval_request_id"])
    [item] = detect(w["acme"], emma, "ATTENDANCE", day)["items"]
    data = proposal(w["acme"], item["proposal_id"])
    days = (date.fromisoformat(w["period"]["end"]) - date.fromisoformat(w["period"]["start"])).days + 1
    # By hand (calendar-day basis): one day of each LOP-applicable component, rounded per line.
    expected = {code: -(Decimal(monthly) / days).quantize(Decimal("0.01"))
                for code, monthly in (("BASIC", 50000), ("HRA", 20000), ("FIXED_ALLOWANCE", 30000))}
    assert deltas(data) == expected
    assert {l["direction"] for l in data["lines"]} == {"RECOVERY"}
    # The maker cannot approve; the approver can.
    assert w["acme"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 403
    r = w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={"comment": "Confirmed absence"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    approved = proposal(w["acme"], item["proposal_id"])
    assert approved["payment_mode"] == "NEXT_REGULAR_RUN" and approved["recovery_mode"] == "NEXT_RUN"
    assert all(len(l["instalments"]) == 1 for l in approved["lines"])
    # The next regular run takes the recovery within the protected net and settles it exactly once.
    following = new_run(w["acme"], w["group"], next_period(w)["id"])
    pay(w, following["id"])
    paid_row = run_employee_id(following["id"], emma)
    recovered = sql("SELECT amount, sources FROM payroll_lines WHERE run_employee_id = :r "
                    "AND component_code = 'ARREARS_RECOVERY' ORDER BY id", r=paid_row)
    assert sum(Decimal(a) for a, _ in recovered) == -sum(expected.values())
    settled = proposal(w["acme"], item["proposal_id"])
    assert settled["status"] == "SETTLED"
    assert {i["status"] for l in settled["lines"] for i in l["instalments"]} == {"SETTLED"}
    consumed = sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND source_type = 'ARREARS' "
                   "AND consumed", r=following["id"])[0][0]
    assert consumed == len(expected)
    # Re-delivering the finalized event changes nothing (exactly once).
    from app.payroll.services import retro, runs
    from tests.payroll.test_runs_lifecycle import session_for
    db = session_for(w["cid"])
    try:
        assert retro.settle_run(db, runs.load_run(db, w["cid"], following["id"]))["settled"] == 0
    finally:
        db.close()
    # The corrected view of the original period = original + delta.
    view = w["acme"].get(f"{RUNS}/{w['run']['id']}/employees/{row}/corrected-view").json()
    assert Decimal(view["corrected"]["BASIC"]["amount"]) == Decimal("50000.00") + expected["BASIC"]
    assert Decimal(view["original"]["BASIC"]["amount"]) == Decimal("50000.00")


def test_ec_lv_03_absence_excused_after_payroll_refunds_the_lop(retro_world, app):
    """EC-LV-03: the LOP paid in the finalized period is withdrawn (leave approved instead): LOP refunded."""
    w = retro_world
    ivan = w["ivan"]
    day = working_day(w["period"])
    entry = lop(w["acme"], ivan, day)
    approve_request(w["hr"], entry["approval_request_id"])
    # The absence is first corrected into the finalized period (approved recovery), then excused.
    [first] = detect(w["acme"], ivan, "ATTENDANCE", day)["items"]
    assert w["approver"].post(f"{ARREARS}/{first['proposal_id']}/approve", json={}).status_code == 200
    r = w["acme"].post(f"{PAY}/lop-inputs/{entry['id']}/cancel")
    assert r.status_code == 200, r.text
    [again] = detect(w["acme"], ivan, "ATTENDANCE", day)["items"]
    refund = proposal(w["acme"], again["proposal_id"])
    days = (date.fromisoformat(w["period"]["end"]) - date.fromisoformat(w["period"]["start"])).days + 1
    assert deltas(refund)["BASIC"] == (Decimal(50000) / days).quantize(Decimal("0.01"))
    assert {l["direction"] for l in refund["lines"]} == {"ARREARS"}
    assert refund["base_proposal_id"] == first["proposal_id"]  # built on the approved correction


def test_ec_lv_04_a_change_that_leaves_pay_unchanged_proposes_nothing(retro_world):
    """EC-LV-04: leave cancelled after payroll while the employee worked: the period is recalculated and,
    with nothing different, no proposal is recorded."""
    w = retro_world
    result = detect(w["acme"], w["emma"], "LEAVE", w["period"]["start"])
    assert [i["status"] for i in result["items"]] == ["NO_DIFFERENCE"]
    assert sql("SELECT count(*) FROM arrears_proposals WHERE company_id = :c", c=w["cid"])[0][0] == 0


# ---------------------------------------------------------------------------
# Supersede, reject, preview, auto-compute switch
# ---------------------------------------------------------------------------
def test_a_newer_change_supersedes_the_pending_proposal_and_keeps_its_sources(retro_world):
    w = retro_world
    emma = w["emma"]
    assign_and_approve(w["acme"], w["approver"], emma, w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [first] = detect(w["acme"], emma, "COMPENSATION", w["period"]["start"])["items"]
    day = working_day(w["period"])
    approve_request(w["hr"], lop(w["acme"], emma, day)["approval_request_id"])
    [second] = detect(w["acme"], emma, "ATTENDANCE", day)["items"]
    assert second["superseded_id"] == first["proposal_id"]
    old, new = proposal(w["acme"], first["proposal_id"]), proposal(w["acme"], second["proposal_id"])
    assert old["status"] == "SUPERSEDED" and old["superseded_by_id"] == new["id"]
    assert [c["type"] for c in new["changed_sources"]] == ["COMPENSATION", "ATTENDANCE"]
    days = (date.fromisoformat(w["period"]["end"]) - date.fromisoformat(w["period"]["start"])).days + 1
    # 62,500 x (days-1)/days - 50,000 for BASIC: the raise and the LOP together.
    assert deltas(new)["BASIC"] == (Decimal(62500) * (days - 1) / days).quantize(Decimal("0.01")) - Decimal(50000)
    assert old["approval_request_id"] and sql("SELECT status FROM approval_requests WHERE id = :r",
                                              r=old["approval_request_id"])[0][0] == "CANCELLED"
    # Only one pending proposal per period (partial unique index).
    assert sql("SELECT count(*) FROM arrears_proposals WHERE original_run_employee_id = :r "
               "AND status = 'PENDING_APPROVAL'", r=w["emma_row"])[0][0] == 1


def test_reject_needs_a_reason_and_preview_records_nothing(retro_world):
    w = retro_world
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    recorded = sql("SELECT count(*) FROM arrears_proposals WHERE company_id = :c", c=w["cid"])[0][0]
    assert recorded == 1  # the compensation hook's proposal
    preview = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"], preview=True)
    [item] = preview["items"]
    assert {l["component_code"]: l["delta"]["amount"] for l in item["lines"]}["BASIC"] == "12500.00"
    assert sql("SELECT count(*) FROM arrears_proposals WHERE company_id = :c", c=w["cid"])[0][0] == recorded
    [created] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    r = w["approver"].post(f"{ARREARS}/{created['proposal_id']}/approve", json={"row_version": 99})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"  # optimistic concurrency
    r = w["approver"].post(f"{ARREARS}/{created['proposal_id']}/reject", json={"reason": " "})
    assert r.status_code == 422
    r = w["approver"].post(f"{ARREARS}/{created['proposal_id']}/reject", json={"reason": "Wrong effective date"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED", r.text
    data = proposal(w["acme"], created["proposal_id"])
    assert data["decision_note"] == "Wrong effective date" and data["lines"][0]["instalments"] == []
    r = w["approver"].post(f"{ARREARS}/{created['proposal_id']}/approve", json={})
    assert r.status_code == 409 and r.json()["code"] == "ARREARS_NOT_PENDING"


def test_automatic_arrears_off_proposes_nothing(retro_world):
    w = retro_world
    set_tenant_setting(w, "payroll.arrears.auto_compute", False)
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    assert item["proposal_id"] is None and item["issues"] == ["ARREARS_AUTO_OFF"]


def test_detect_validation_and_permissions(retro_world):
    w = retro_world
    detect(w["acme"], w["emma"], "MANUAL", w["period"]["start"], expect=422)
    detect(w["acme"], w["emma"], "SETTING", w["period"]["start"], expect=400)  # KEYS_REQUIRED
    detect(w["approver"], w["emma"], "COMPENSATION", w["period"]["start"], expect=403)  # no payroll:run:create
    r = w["acme"].post(f"{ARREARS}/detect", json={"employee_id": w["emma"], "trigger": "COMPENSATION",
                                                  "from_date": w["period"]["start"], "reason": "x",
                                                  "company_id": w["cid"]})
    assert r.status_code == 422  # server-owned fields are refused


# ---------------------------------------------------------------------------
# Recovery beyond the protected net (EC-COR-05)
# ---------------------------------------------------------------------------
def test_ec_cor_05_plan_recovery_splits_by_the_protected_net_capacity():
    """EC-COR-17: recovery lines never exceed the protected net capacity."""
    from app.payroll.services.retro import instalment_count, plan_recovery, split_amount

    plan = plan_recovery("NEXT_RUN", Decimal("60000"), gross=Decimal("100000"), net=Decimal("100000"),
                         min_net_percent=Decimal("50"), currency="INR")
    assert (plan.parts, plan.capacity, plan.recovery_mode) == (2, Decimal("50000.00"), "INSTALMENTS")
    assert plan_recovery("NEXT_RUN", Decimal("100"), gross=Decimal("1000"), net=Decimal("900"),
                         min_net_percent=Decimal("50"), currency="INR").parts == 1
    manual = plan_recovery("NEXT_RUN", Decimal("100"), gross=Decimal("1000"), net=Decimal("900"),
                           min_net_percent=None, currency="INR")
    assert manual.recovery_mode == "MANUAL" and manual.issues[0]["code"] == "PROTECTED_NET_REQUIRED"
    assert plan_recovery("MANUAL", Decimal("1"), gross=None, net=None, min_net_percent=None,
                         currency="INR").recovery_mode == "MANUAL"
    assert split_amount(Decimal("-100.00"), 3, "INR") == [Decimal("-33.33"), Decimal("-33.33"), Decimal("-33.34")]
    assert instalment_count(Decimal("100000.01"), Decimal("50000")) == 3


def test_ec_cor_05_a_recovery_larger_than_one_run_is_split_into_instalments(retro_world):
    w = retro_world
    set_tenant_setting(w, "payroll.deductions.min_net_pay_percent", "80")
    set_tenant_setting(w, "payroll.arrears.recovery_mode", "NEXT_RUN")
    emma = w["emma"]
    start = date.fromisoformat(w["period"]["start"])
    taken = 0
    for offset in range(0, 28):
        day = start + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        r = w["acme"].post(f"{PAY}/lop-inputs", json={"employee_id": emma, "date": str(day), "portion": "FULL",
                                                      "reason": "Unauthorised absence"})
        if r.status_code != 201:  # holidays or weekly offs of the work week
            continue
        approve_request(w["hr"], r.json()["approval_request_id"])
        taken += 1
    assert taken >= 8
    [item] = detect(w["acme"], emma, "ATTENDANCE", start)["items"]
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    data = proposal(w["acme"], item["proposal_id"])
    recovery = -sum(deltas(data).values())
    capacity = Decimal("20000.00")  # by hand: net 1,00,000 - 80% of gross 1,00,000
    assert recovery > capacity
    expected_parts = int((recovery / capacity).to_integral_value(rounding="ROUND_CEILING"))
    assert data["recovery_mode"] == "INSTALMENTS"
    assert any(i["code"] == "RECOVERY_SPLIT" for i in data["issues"])
    for line in data["lines"]:
        parts = [Decimal(i["amount"]["amount"]) for i in line["instalments"]]
        assert len(parts) == expected_parts >= 2 and sum(parts) == Decimal(line["delta"]["amount"])
    # Only the first instalment of each line is offered to the next run.
    from app.payroll.services import retro, runs
    from tests.payroll.test_runs_lifecycle import session_for
    following = new_run(w["acme"], w["group"], next_period(w)["id"])
    db = session_for(w["cid"])
    try:
        from app.models.pay_runs import PayrollRunEmployee
        run = runs.load_run(db, w["cid"], following["id"])
        row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id,
                                                  PayrollRunEmployee.employee_id == emma).first()
        offered = retro.arrears_for_snapshot(db, run, row)
    finally:
        db.close()
    assert len(offered) == len(data["lines"])
    assert sum(Decimal(i["amount"]) for i in offered) == sum(
        Decimal(l["instalments"][0]["amount"]["amount"]) for l in data["lines"])


def test_manual_recovery_mode_is_a_receivable_never_offered_to_a_run(retro_world):
    w = retro_world
    set_tenant_setting(w, "payroll.arrears.recovery_mode", "MANUAL")
    day = working_day(w["period"])
    approve_request(w["hr"], lop(w["acme"], w["emma"], day)["approval_request_id"])
    [item] = detect(w["acme"], w["emma"], "ATTENDANCE", day)["items"]
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    data = proposal(w["acme"], item["proposal_id"])
    assert data["recovery_mode"] == "MANUAL" and Decimal(data["outstanding"]["amount"]) < 0
    following = new_run(w["acme"], w["group"], next_period(w)["id"])
    pay(w, following["id"])
    row = run_employee_id(following["id"], w["emma"])
    assert sql("SELECT count(*) FROM payroll_lines WHERE run_employee_id = :r AND component_code LIKE 'ARREARS%'",
               r=row)[0][0] == 0
    assert proposal(w["acme"], item["proposal_id"])["status"] == "APPROVED"


# ---------------------------------------------------------------------------
# RETRO_ADJUSTMENT runs (payment mode OFF_CYCLE)
# ---------------------------------------------------------------------------
def test_off_cycle_payment_mode_waits_for_a_retro_adjustment_run_that_claims_once(retro_world):
    w = retro_world
    set_tenant_setting(w, "payroll.arrears.payment_mode", "OFF_CYCLE")
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    assert proposal(w["acme"], item["proposal_id"])["payment_mode"] == "OFF_CYCLE"
    nxt = next_period(w)
    body = {"pay_group_id": w["group"], "pay_period_id": nxt["id"], "proposal_ids": [item["proposal_id"]]}
    r = w["acme"].post(f"{PAY}/retro-runs", json=body)
    assert r.status_code == 201 and r.json()["run_type"] == "RETRO_ADJUSTMENT", r.text
    retro_run = r.json()
    selection = sql("SELECT selection FROM payroll_runs WHERE id = :r", r=retro_run["id"])[0][0]
    assert selection["original_period_ids"] == [w["period"]["id"]]
    assert w["acme"].post(f"{PAY}/retro-runs", json=body).status_code == 409  # already claimed
    # A regular run of the same period does not pay it; the retro run does.
    regular = new_run(w["acme"], w["group"], nxt["id"])
    pay(w, regular["id"])
    assert proposal(w["acme"], item["proposal_id"])["status"] == "APPROVED"
    pay(w, retro_run["id"])
    assert proposal(w["acme"], item["proposal_id"])["status"] == "SETTLED"
    paid = lines_of(run_employee_id(retro_run["id"], w["emma"]))
    assert paid["ARREARS"] == Decimal("25000.00")


def test_retro_run_needs_approved_proposals_of_the_pay_group(retro_world):
    w = retro_world
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    body = {"pay_group_id": w["group"], "pay_period_id": next_period(w)["id"], "proposal_ids": [item["proposal_id"]]}
    r = w["acme"].post(f"{PAY}/retro-runs", json=body)
    assert r.status_code == 409 and r.json()["code"] == "ARREARS_NOT_APPROVED"
    r = w["acme"].post(f"{PAY}/retro-runs", json={**body, "proposal_ids": [999999]})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Settings hook (EC-CFG-03)
# ---------------------------------------------------------------------------
def test_ec_cfg_03_a_past_dated_setting_in_a_finalized_period_is_detected(retro_world):
    from app.payroll.services import settings as settings_engine
    from tests.payroll.test_runs_lifecycle import session_for

    w = retro_world
    start = date.fromisoformat(w["period"]["start"])
    db = session_for(w["cid"])
    try:
        assert settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.lop.proration_basis",
                                                         settings_engine.Level.TENANT, None, start)
        assert settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.lop.proration_basis",
                                                         settings_engine.Level.PAY_GROUP, w["group"], start)
        assert not settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.lop.proration_basis",
                                                             settings_engine.Level.PAY_GROUP, w["ae_group"], start)
        later = date.fromisoformat(w["period"]["end"]) + timedelta(days=1)
        assert not settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.lop.proration_basis",
                                                             settings_engine.Level.TENANT, None, later)
        assert settings_engine.finalized_period_overlaps(db, w["cid"], "payroll.lop.proration_basis",
                                                         settings_engine.Level.EMPLOYEE, w["emma"], start)
    finally:
        db.close()
    # A past-dated setting is previewed against the finalized period (no difference without LOP days).
    preview = detect(w["acme"], w["emma"], "SETTING", w["period"]["start"], preview=True,
                     keys=["payroll.lop.proration_basis"])
    assert [i["lines"] for i in preview["items"]] == [[]]


# ---------------------------------------------------------------------------
# Scope and three-tenant isolation (EC-SEC-01, EC-SEC-05)
# ---------------------------------------------------------------------------
def test_ec_sec_01_proposals_are_invisible_and_unactionable_across_tenants(retro_world):
    w = retro_world
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    pid = item["proposal_id"]
    for other in (w["beta"]["client"], w["cobalt"]["client"]):
        assert other.get(f"{ARREARS}/{pid}").status_code == 404
        assert other.post(f"{ARREARS}/{pid}/approve", json={}).status_code in (403, 404)  # no approve right
        assert other.post(f"{ARREARS}/{pid}/reject", json={"reason": "no"}).status_code in (403, 404)
        assert other.get(ARREARS).json()["total"] == 0
        # Cross-tenant references: a foreign employee and a foreign run employee.
        r = other.post(f"{ARREARS}/detect", json={"employee_id": w["emma"], "trigger": "COMPENSATION",
                                                  "from_date": w["period"]["start"], "reason": "probe"})
        assert r.status_code == 404, r.text
        r = other.get(f"{RUNS}/{w['run']['id']}/employees/{w['emma_row']}/corrected-view")
        assert r.status_code == 404
        r = other.post(f"{PAY}/retro-runs", json={"pay_group_id": w["group"], "pay_period_id": w["period"]["id"],
                                                  "proposal_ids": [pid]})
        assert r.status_code == 404
    # The entity clerk (AE01 only) does not see LE01 proposals.
    assert w["cleo"].get(f"{ARREARS}/{pid}").status_code == 404
    assert w["cleo"].get(ARREARS).json()["total"] == 0


def test_ec_sec_01_corrections_tables_enforce_row_level_security_for_three_tenants(retro_world):
    w = retro_world
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    for table in CORRECTION_TABLES:
        for company_id in (w["beta"]["cid"], w["cobalt"]["cid"], None):
            with tenant(company_id) as conn:
                from sqlalchemy import text
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
    with tenant(w["cid"]) as conn:
        from sqlalchemy import text
        # The compensation hook's proposal (superseded by the manual detect) and the approved one.
        assert conn.execute(text("SELECT count(*) FROM arrears_proposals")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM arrears_proposals WHERE status = 'APPROVED'")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM arrears_instalments")).scalar() == 3
    # Writing a row for another tenant is refused by the policy.
    from sqlalchemy.exc import DBAPIError
    with tenant(w["beta"]["cid"]) as conn:
        from sqlalchemy import text
        with pytest.raises(DBAPIError) as info:
            with conn.begin_nested():
                conn.execute(text("UPDATE arrears_proposals SET reason = 'x' WHERE id = :i RETURNING id"),
                             {"i": item["proposal_id"]}).fetchall()
                conn.execute(text(
                    "INSERT INTO final_settlement_deadlines (company_id, days, day_basis, row_version, created_at, "
                    "updated_at) VALUES (:c, 5, 'CALENDAR_DAYS', 1, now(), now())"), {"c": w["cid"]})
        assert info.value.orig.pgcode == DENIED


def test_database_rules_on_proposal_lines(retro_world):
    """A line's direction follows the sign of its delta; a delta is never zero (CHECK constraints)."""
    from sqlalchemy.exc import DBAPIError

    w = retro_world
    assign_and_approve(w["acme"], w["approver"], w["emma"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    [item] = detect(w["acme"], w["emma"], "COMPENSATION", w["period"]["start"])["items"]
    with pytest.raises(DBAPIError) as info:
        sql("UPDATE arrears_proposal_lines SET direction = 'RECOVERY' WHERE proposal_id = :p", p=item["proposal_id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError):
        sql("UPDATE arrears_proposal_lines SET delta = 0 WHERE proposal_id = :p", p=item["proposal_id"])
    with pytest.raises(DBAPIError):  # a second pending proposal for the same run employee
        sql("INSERT INTO arrears_proposals (company_id, employee_id, legal_entity_id, pay_group_id, original_run_id, "
            "original_run_employee_id, original_period_id, period_start, period_end, currency, trigger, status, "
            "row_version, created_at, updated_at) SELECT company_id, employee_id, legal_entity_id, pay_group_id, "
            "original_run_id, original_run_employee_id, original_period_id, period_start, period_end, currency, "
            "'MANUAL', 'PENDING_APPROVAL', 1, now(), now() FROM arrears_proposals WHERE id = :p",
            p=item["proposal_id"])


# ---------------------------------------------------------------------------
# Late overtime (EC-OT-07, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_ot_07_overtime_approved_after_finalization_is_paid_as_arrears_and_consumed_once(retro_world):
    from tests.payroll_unit.test_engine_fixtures import overtime_policy

    w = retro_world
    set_tenant_setting(w, "payroll.arrears.recovery_mode", "NEXT_RUN")
    ot = {"settings": {"payroll.overtime.enabled": True}, "policies": {"OVERTIME_POLICY": overtime_policy()}}
    november = next_period(w)
    run = new_run(w["acme"], w["group"], november["id"])
    pay(w, run["id"], **ot)
    day = working_day(november)
    entry = sql("INSERT INTO overtime_entries (company_id, employee_id, date, minutes_requested, minutes_approved, "
                "source, status, comp_off, pre_approval, version, row_version, decided_at, created_at, updated_at) "
                "VALUES (:c, :e, :d, 120, 120, 'APPROVED_REQUESTS', 'APPROVED', false, false, 1, 1, now(), now(), now()) "
                "RETURNING id", c=w["cid"], e=w["emma"], d=day)[0][0]
    [item] = detect(w["acme"], w["emma"], "OVERTIME", day)["items"]
    data = proposal(w["acme"], item["proposal_id"])
    # By hand: 50,000 / 26 / 8 = 240.384615... per hour x 1.5 x 2 hours = 721.15.
    assert deltas(data) == {"OT_WORKDAY": Decimal("721.15")}
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    december = next_period(w, november)
    following = new_run(w["acme"], w["group"], december["id"])
    pay(w, following["id"], **ot)
    assert lines_of(run_employee_id(following["id"], w["emma"]))["ARREARS"] == Decimal("721.15")
    consumed = sql("SELECT consumed_by_run_employee_id FROM overtime_entries WHERE id = :i", i=entry)[0][0]
    assert consumed == run_employee_id(following["id"], w["emma"])
    assert proposal(w["acme"], item["proposal_id"])["status"] == "SETTLED"


# ---------------------------------------------------------------------------
# Late tasks (EC-TSK-03, EC-VAR-01)
# ---------------------------------------------------------------------------
TASKS_ON = {"payroll.task_pay.enabled": True}


def _task_policy(attribution):
    return {"TASK_PAY_POLICY": {"id": 41, "version": 1, "policy_hash": "task-hash",
                                "fields": {"attribution": attribution, "rate_source": "TASK"}}}


def _late_task(w, completed_on, approved_on):
    task = sql("INSERT INTO tasks (company_id, title, task_type, quantity, unit, rate, rate_currency, completed_on, "
               "status, approved_on, row_version, created_at, updated_at) VALUES (:c, 'Site visit', 'SITE_VISIT', 3, "
               "'VISIT', 500, 'INR', :d, 'APPROVED', :a, 1, now(), now()) RETURNING id",
               c=w["cid"], d=completed_on, a=approved_on)[0][0]
    sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) "
        "VALUES (:c, :t, :e, 100, now(), now())", c=w["cid"], t=task, e=w["emma"])
    return task


def test_ec_tsk_03_task_completed_in_a_finalized_period_is_paid_as_arrears_with_completion_attribution(retro_world):
    w = retro_world
    november = next_period(w)
    run = new_run(w["acme"], w["group"], november["id"])
    pay(w, run["id"], settings=TASKS_ON, policies=_task_policy("COMPLETION_DATE"))
    task = _late_task(w, working_day(november), date.fromisoformat(november["end"]) + timedelta(days=3))
    [item] = detect(w["acme"], w["emma"], "TASK", november["start"])["items"]
    data = proposal(w["acme"], item["proposal_id"])
    assert deltas(data) == {"TASK_EARNING": Decimal("1500.00")}  # by hand: 3 visits x 500
    assert data["lines"][0]["period_ref"]["period_id"] == november["id"]
    assert w["approver"].post(f"{ARREARS}/{item['proposal_id']}/approve", json={}).status_code == 200
    december = next_period(w, november)
    following = new_run(w["acme"], w["group"], december["id"])
    pay(w, following["id"], settings=TASKS_ON, policies=_task_policy("COMPLETION_DATE"))
    assert lines_of(run_employee_id(following["id"], w["emma"]))["ARREARS"] == Decimal("1500.00")
    # Paid exactly once: by the run that paid the arrears (EC-VAR-01).
    assert sql("SELECT status, paid_in_run_id FROM tasks WHERE id = :t", t=task)[0] == ("PAID", following["id"])
    assert proposal(w["acme"], item["proposal_id"])["status"] == "SETTLED"


def test_ec_tsk_03_with_approval_date_attribution_a_late_task_belongs_to_the_next_period(retro_world):
    w = retro_world
    november = next_period(w)
    run = new_run(w["acme"], w["group"], november["id"])
    pay(w, run["id"], settings=TASKS_ON, policies=_task_policy("APPROVAL_DATE"))
    _late_task(w, working_day(november), date.fromisoformat(november["end"]) + timedelta(days=3))
    result = detect(w["acme"], w["emma"], "TASK", november["start"])
    assert [i["status"] for i in result["items"]] == ["NO_DIFFERENCE"]
    assert sql("SELECT count(*) FROM arrears_proposals WHERE company_id = :c", c=w["cid"])[0][0] == 0
