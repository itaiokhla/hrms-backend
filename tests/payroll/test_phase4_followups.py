"""Open findings of the phase-4 adversarial reviews (FOLLOW-UPS, key p4-followups).

1. EC-TSK-* / EC-VAR-01: a task shared by assignees is paid once per assignee (``task_assignees.paid_in_run_id``,
   migration 0028): calculation and finalization key the TASK line source ``employee:<id>``, so the assignees'
   shares can be paid by different runs, each exactly once; the task turns PAID with its last share.
2. EC-RUN-09: the full change-detection pass (claims, loans, bank accounts, YTD are not in the source change log)
   runs at submission and finalization; a change marks the run stale (kept although the request is refused).
3. No salary data in logs: the CLOSE transition audit names carried holds by ids and reason codes only.
5. EC-COR-06: inputs an off-cycle selection leaves out carry no source hash, so their changes never mark the
   off-cycle run stale; a selected input still does.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.payroll.test_components import ok
from tests.payroll.test_run_calculation import calculate as calculate_job
from tests.payroll.test_run_calculation import findings, key, no_pack_country, precheck_ready
from tests.payroll.test_run_calculation import run_state as calc_state
from tests.payroll.test_run_finalize import calculate as fake_calculate
from tests.payroll.test_run_finalize import (  # noqa: F401 (fin_world is a fixture)
    ATTESTATIONS,
    approve,
    approved_run,
    code_of,
    confirm_countries,
    fin_world,
    finalize,
    line,
    step_up,
    submit,
)
from tests.payroll.test_runs_lifecycle import (  # noqa: F401 (runs_world is a fixture)
    RUNS,
    force_status,
    new_run,
    periods_of,
    run_employees,
    runs_world,
    session_for,
)
from tests.payroll.test_snapshot_builder import WAGE_INPUTS, emma as emma_of, lock
from tests.payroll.test_wage_inputs import component, set_setting, sql


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def shared_task(w, shares, *, on=None):
    """An APPROVED task split between assignees: ``shares`` is [(employee id, split percent)]."""
    task = sql("INSERT INTO tasks (company_id, title, task_type, quantity, unit, status, approved_on, decided_at, "
               "row_version, created_at, updated_at) VALUES (:c, 'Install meters', 'INSTALL', 2, 'unit', 'APPROVED', "
               ":d, now(), 1, now(), now()) RETURNING id", c=w["cid"], d=on or w["period"]["start"])[0][0]
    for employee_id, split in shares:
        sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) "
            "VALUES (:c, :t, :e, :s, now(), now())", c=w["cid"], t=task, e=employee_id, s=Decimal(split))
    return task


def task_line(task, amount):
    return line("TASK_EARNING", amount, category="TASK_EARNING", sources=[{"type": "task", "id": f"task:{task}"}])


def shares_of(task):
    return dict(sql("SELECT employee_id, paid_in_run_id FROM task_assignees WHERE task_id = :t", t=task))


def task_state(task):
    return sql("SELECT status, paid_in_run_id FROM tasks WHERE id = :t", t=task)[0]


def approved_for(w, employee_id, start, end):
    from app.payroll.services import tasks

    db = session_for(w["cid"])
    try:
        return tasks.approved_tasks(db, w["cid"], employee_id, start, end, "APPROVAL_DATE")
    finally:
        db.close()


def stale_of(run_id):
    return sql("SELECT stale, stale_reasons FROM payroll_runs WHERE id = :r", r=run_id)[0]


# ---------------------------------------------------------------------------
# 1. Multi-assignee tasks: one paid marker per assignee (EC-TSK-*, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_tsk_ec_var_01_task_line_source_keys_match_between_calculation_and_finalization():
    """The calculation writes the TASK line source with the key finalization consumes ('employee:<id>'); it used
    to write '' so a second assignee's run collided with the first one's consumption."""
    from app.payroll.services import calculation, finalization

    data = {"sources": [{"type": "task", "id": "task:41"}, {"type": "wage_input", "id": 7},
                        {"type": "loan", "id": 3, "instalment_number": 2}]}
    written = calculation._source_rows(data, 12)
    assert ("TASK", 41, "employee:12") in written
    assert ("WAGE_INPUT", 7, "") in written and ("LOAN", 3, "instalment:2") in written
    consumed = finalization.parse_sources(SimpleNamespace(sources=data["sources"], kind="EARNING"), 12)
    assert ("TASK", 41, "employee:12") in {(kind, ident, k) for kind, ident, k, _ in consumed}
    assert calculation._source_rows(data, 13) != written      # another assignee: another key


def test_ec_tsk_ec_var_01_each_share_is_marked_paid_once_and_the_task_turns_paid_with_the_last(runs_world):
    from app.core.errors import AppError
    from app.payroll.services import tasks

    w = runs_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    task = shared_task(w, [(emma, "60"), (ivan, "40")])
    before_ivan = approved_for(w, ivan, start, end)
    assert [t["id"] for t in before_ivan] == [task] and before_ivan[0]["paid_in_run_id"] is None
    db = session_for(w["cid"])
    try:
        assert tasks.mark_task_shares_paid(db, w["cid"], [(task, emma)], 9001) == 1
        assert tasks.mark_task_shares_paid(db, w["cid"], [(task, emma)], 9001) == 0     # idempotent
        db.commit()
    finally:
        db.close()
    assert task_state(task) == ("APPROVED", None) and shares_of(task) == {emma: 9001, ivan: None}
    assert approved_for(w, emma, start, end) == []                       # Emma's share is never paid again
    after_ivan = approved_for(w, ivan, start, end)
    assert after_ivan == before_ivan    # same content hash: Ivan's run is not marked stale by Emma's payment
    db = session_for(w["cid"])
    try:
        try:
            tasks.mark_task_shares_paid(db, w["cid"], [(task, emma)], 9002)
            raise AssertionError("a share was paid twice")
        except AppError as error:
            assert error.status_code == 409 and error.code == "TASK_SHARE_ALREADY_PAID"
        db.rollback()
        try:
            tasks.mark_task_shares_paid(db, w["cid"], [(task, w["gus"]["id"])], 9002)
            raise AssertionError("a non-assignee was paid")
        except AppError as error:
            assert error.status_code == 409 and error.code == "TASK_SHARE_NOT_FOUND"
        db.rollback()
        assert tasks.mark_task_shares_paid(db, w["cid"], [(task, ivan)], 9002) == 1
        db.commit()
    finally:
        db.close()
    assert task_state(task) == ("PAID", 9002) and shares_of(task) == {emma: 9001, ivan: 9002}
    assert approved_for(w, ivan, start, end) == []


def test_ec_tsk_ec_var_01_assignees_paid_by_different_runs_each_exactly_once(fin_world):
    """Emma's share is paid by this period's run, Ivan's by the next period's run; a third run that would pay
    Emma's share again is refused (409 SOURCE_ALREADY_CONSUMED) and nothing is paid twice."""
    w = fin_world
    acme = w["acme"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    task = shared_task(w, [(emma, "60"), (ivan, "40")])

    first = approved_run(w, prepared={emma: [line("BASIC", "50000.00"), task_line(task, "240.00")]})
    finalize(acme, first["id"], key="task-share-1")
    assert task_state(task) == ("APPROVED", None)                      # Ivan's share is still unpaid
    assert shares_of(task) == {emma: first["id"], ivan: None}
    consumed = sql("SELECT run_id, source_key FROM payroll_line_sources WHERE source_type = 'TASK' AND consumed "
                   "AND source_id = :t", t=task)
    assert consumed == [(first["id"], f"employee:{emma}")]

    periods = sorted((p for p in periods_of(acme, w["group"]) if p["start"] > w["period"]["end"]),
                     key=lambda p: p["start"])
    second = approved_run(w, period=periods[0], prepared={ivan: [line("BASIC", "50000.00"),
                                                                 task_line(task, "160.00")]})
    finalize(acme, second["id"], key="task-share-2")
    assert task_state(task) == ("PAID", second["id"])
    assert shares_of(task) == {emma: first["id"], ivan: second["id"]}
    assert sorted(sql("SELECT run_id, source_key FROM payroll_line_sources WHERE source_type = 'TASK' AND consumed "
                      "AND source_id = :t", t=task)) == sorted([(first["id"], f"employee:{emma}"),
                                                                (second["id"], f"employee:{ivan}")])

    third = approved_run(w, period=periods[1], prepared={emma: [line("BASIC", "50000.00"),
                                                                task_line(task, "240.00")]})
    r = finalize(acme, third["id"], key="task-share-3", expect=409)
    assert code_of(r) == "SOURCE_ALREADY_CONSUMED"
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=third["id"])[0][0] == "APPROVED"
    assert shares_of(task) == {emma: first["id"], ivan: second["id"]}


# ---------------------------------------------------------------------------
# 2. Full change detection at submission and finalization (EC-RUN-09)
# ---------------------------------------------------------------------------
def _ytd(w, employee_id, bucket, amount):
    tax_year = sql("SELECT tax_year FROM pay_periods WHERE id = :p", p=w["period"]["id"])[0][0]
    return sql("INSERT INTO ytd_balances (company_id, employee_id, legal_entity_id, tax_year, bucket, amount, currency, "
               "row_version, created_at, updated_at) VALUES (:c, :e, :le, :y, :b, :a, 'INR', 1, now(), now()) "
               "RETURNING id", c=w["cid"], e=employee_id, le=w["le01"], y=tax_year, b=bucket, a=amount)[0][0]


def _detect(w, run_id, **kwargs):
    from app.payroll.services import change_detection
    from tests.payroll.test_snapshot_builder import objects

    db = session_for(w["cid"])
    try:
        run, _ = objects(db, run_id)
        return change_detection.detect_changes(db, run, **kwargs)
    finally:
        db.close()


def test_ec_run_09_full_change_detection_gates_submission_and_finalization(app, runs_world):
    """A YTD balance (never in the source change log) written after the inputs were locked: the hourly log-based
    pass misses it, the submission gate finds it, marks the run stale (the marker survives the 409) and the
    run submits once recalculated; a change between approval and finalization is caught by the finalize gate."""
    from tests.helpers import create_employee
    from tests.payroll.test_compensation import login_as

    w = runs_world
    acme, cid = w["acme"], w["cid"]
    emma = w["emp"]["Emma"]["id"]
    no_pack_country(w)
    confirm_countries(cid)
    set_setting(cid, "payroll.attendance.enabled", False)
    attestation = ok(acme.post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                   "reason": "Statutory filings handled by our UK bureau"}), 201)
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert calc_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])

    balance = _ytd(w, emma, "TOTAL:GROSS", "1000.0000")
    assert _detect(w, run["id"]) == {}                                 # not in the change log
    r = acme.post(f"{RUNS}/{run['id']}/submit", json={})
    assert r.status_code == 409 and code_of(r) == "RUN_STALE", r.text
    stale, reasons = stale_of(run["id"])
    assert stale and reasons[-1]["source"] == "change_detection" and reasons[-1]["employee_ids"] == [emma]
    assert {"employee_id": emma, "source": f"ytd_balance:{balance}", "type": "ytd_balance",
            "change": "ADDED"} in reasons[-1]["changes"]
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] == "REVIEW_REQUIRED"

    calculate_job(acme, run["id"])                                    # recalculation clears the stale marker
    assert stale_of(run["id"])[0] is False and calc_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    submit(acme, run["id"])                                           # the clean full pass lets it through
    approve(w["approver"], run["id"])
    assert calc_state(run["id"])[0] == "APPROVED"

    _ytd(w, emma, "TOTAL:NET", "900.0000")
    step_up(acme)
    r = finalize(acme, run["id"], key=key(), expect=409)
    assert code_of(r) == "RUN_STALE"
    stale, reasons = stale_of(run["id"])
    assert stale and any(c["type"] == "ytd_balance" for c in reasons[-1]["changes"])
    assert calc_state(run["id"])[0] == "APPROVED"                      # nothing was finalized or paid
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=run["id"])[0][0] == 0


def test_ec_run_09_the_gate_skips_reversal_runs_and_snapshots_without_sources(runs_world):
    from app.payroll.services import change_detection
    from tests.payroll.test_snapshot_builder import objects

    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(run["id"], "READY")
    lock(w["cid"], run["id"], [emma_of(w)])
    force_status(run["id"], "REVIEW_REQUIRED")
    _ytd(w, emma_of(w), "TOTAL:GROSS", "10.0000")
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, run["id"])
        found.run_type = "REVERSAL"                                    # in memory only: rolled back below
        assert change_detection.gate_full_check(db, found, "submission")["checked"] is False
        db.rollback()
    finally:
        db.close()
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, run["id"])
        with pytest.raises(change_detection.RunMarkedStale) as info:          # a real snapshot: compared
            change_detection.gate_full_check(db, found, "submission")
        assert info.value.status_code == 409 and info.value.code == "RUN_STALE"
        db.rollback()                                                        # the route commits; the service never
    finally:
        db.close()
    assert stale_of(run["id"])[0] is False
    # Snapshots without any source hash (not frozen from live sources, e.g. imported) have nothing to compare.
    following = min((p for p in periods_of(w["acme"], w["group"]) if p["start"] > w["period"]["end"]),
                    key=lambda p: p["start"])
    other = new_run(w["acme"], w["group"], following["id"])
    fake_calculate(w["acme"], other["id"])
    assert sql("SELECT count(*) FROM payroll_input_snapshots s JOIN payroll_run_employees e ON e.snapshot_id = s.id "
               "WHERE e.run_id = :r AND s.source_hashes <> '{}'::jsonb", r=other["id"])[0][0] == 0
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, other["id"])
        assert change_detection.gate_full_check(db, found, "submission")["marked"] is False
        db.rollback()
    finally:
        db.close()
    assert stale_of(other["id"])[0] is False


def test_ec_run_09_i15_an_attestation_approved_after_lock_does_not_trip_the_gate(app, runs_world):
    """The attestation only decides the statutory gate, which finalization re-reads live (I-15): approving one
    after the lock is how a run of an entity without a verified pack becomes finalizable, not a stale input."""
    from app.payroll.services import change_detection
    from tests.helpers import create_employee
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_snapshot_builder import objects

    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(run["id"], "READY")
    lock(w["cid"], run["id"], [emma_of(w)])
    force_status(run["id"], "REVIEW_REQUIRED")
    attestation = ok(w["acme"].post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                        "reason": "Statutory filings by our external provider"}), 201)
    checker = login_as(app, w["acme"], create_employee(w["acme"], "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    changes = _detect(w, run["id"], full=True)
    assert {c["type"] for c in changes[emma_of(w)]} == {"attestation"}      # seen, and nothing else changed
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, run["id"])
        assert change_detection.gate_full_check(db, found, "finalization")["marked"] is False
        db.rollback()
    finally:
        db.close()
    assert stale_of(run["id"])[0] is False


# ---------------------------------------------------------------------------
# 3. CLOSE audit without salary data
# ---------------------------------------------------------------------------
def test_close_audit_names_carried_holds_without_amounts(fin_world):
    w = fin_world
    acme = w["acme"]
    run = approved_run(w, prepared={})
    ivan = w["emp"]["Ivan"]["id"]
    ivan_row = run_employees(run["id"])[ivan][2]
    r = acme.post(f"{RUNS}/{run['id']}/employees/{ivan_row}/hold",
                  json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE", "note": "Account closed"})
    assert r.status_code == 200, r.text
    finalize(acme, run["id"], key="close-audit-1")
    force_status(run["id"], "RECONCILED")
    closed = ok(acme.post(f"{RUNS}/{run['id']}/close", json={}))
    assert closed["status"] == "CLOSED" and all("net" not in h for h in closed["carried_holds"])
    rows = sql("SELECT details FROM audit_logs WHERE company_id = :c AND entity_type = 'payroll_run' "
               "AND entity_id = :r AND details::jsonb ? 'carried_holds'", c=w["cid"], r=str(run["id"]))
    assert rows, "the CLOSE transition is audited"
    for (details,) in rows:
        assert details["carried_holds"] == [{"run_employee_id": ivan_row, "employee_id": ivan,
                                             "reason_code": "BANK_ISSUE"}]
        assert "50000" not in str(details)                             # Ivan's net is not in the audit


# ---------------------------------------------------------------------------
# 5. Off-cycle selection: excluded inputs never mark the run stale (EC-COR-06)
# ---------------------------------------------------------------------------
def _wage_input(w, employee_id, code, amount):
    start, end = w["period"]["start"], w["period"]["end"]
    r = w["acme"].post(WAGE_INPUTS, json={"employee_id": employee_id, "component_code": code, "period_start": start,
                                          "period_end": end, "amount": {"amount": amount, "currency": "INR"}})
    assert r.status_code == 201, r.text
    return r.json()["items"][0]["id"]


def test_ec_cor_06_inputs_left_out_by_an_off_cycle_selection_never_mark_it_stale(runs_world):
    w = runs_world
    emma = emma_of(w)
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    component(w["cid"], "SPOT_BONUS")
    component(w["cid"], "SPOT_COMMISSION")
    bonus = _wage_input(w, emma, "SPOT_BONUS", "2500.00")
    commission = _wage_input(w, emma, "SPOT_COMMISSION", "800.00")
    r = w["acme"].post("/api/v1/payroll/off-cycle-runs", json={
        "kind": "BONUS", "reason": "Diwali bonus", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"],
        "employee_ids": [emma], "component_codes": ["SPOT_BONUS"]})
    assert r.status_code == 201, r.text
    bonus_run = r.json()["id"]
    force_status(bonus_run, "READY")
    [stored] = lock(w["cid"], bonus_run, [emma]).snapshots.values()
    force_status(bonus_run, "REVIEW_REQUIRED")
    assert f"wage_input:{bonus}" in stored.source_hashes             # the selected input is watched
    assert f"wage_input:{commission}" not in stored.source_hashes     # the excluded one is not
    assert _detect(w, bonus_run, full=True) == {}

    _wage_input(w, emma, "SPOT_COMMISSION", "300.00")                 # another excluded input: ignored
    sql("UPDATE wage_inputs SET amount = 900 WHERE id = :i", i=commission)
    assert _detect(w, bonus_run, full=True) == {}
    added = _wage_input(w, emma, "SPOT_BONUS", "100.00")               # a selected input: the run is stale
    changes = _detect(w, bonus_run, full=True)
    assert {"source": f"wage_input:{added}", "type": "wage_input", "change": "ADDED"} in changes[emma]
    assert all(c["source"] != f"wage_input:{commission}" for c in changes[emma])
