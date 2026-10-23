"""Independent skeptic checks of the phase-4 follow-up fixes (key p4-verify).

Each test tries to refute one fix with a path the fixer's own tests (test_phase4_followups.py) do not walk:

1. EC-TSK-* / EC-VAR-01 (per-assignee task payment): assignees in two pay groups whose runs are open at the same
   time and finalize in the "wrong" order, both assignees in one run, a share already paid elsewhere (the whole
   finalization must roll back), change detection of one assignee while the other is paid, a finalize retry,
   and the retro late-source path (paying only the arrears employee's share).
2. EC-RUN-09 (full change detection at the gates): a refused finalization stays refused on a retry with the
   same Idempotency-Key (no cached success, no second stale reason); the attestation filter never hides a real
   change made together with it.
3. CLOSE audit without salary data: a FINALIZED run whose every employee is payment-held closes directly; the
   audit names both holds by ids and reason codes, never the nets.
5. EC-COR-06 (off-cycle selection): an excluded task (and its rate card) carries no hash, an excluded wage input
   moved into a selected component is detected; the regular run's change detection still marks the bonus run
   stale through ``runs.mark_stale``'s pay-group-wide marking (runs.py, not owned here: strict xfail).
"""

import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import ok
from tests.payroll.test_phase4_followups import (
    _detect,
    _wage_input,
    _ytd,
    shared_task,
    shares_of,
    stale_of,
    task_line,
    task_state,
)
from tests.payroll.test_run_calculation import calculate as calculate_job
from tests.payroll.test_run_calculation import findings, lines_of, no_pack_country, precheck_ready, rows_of
from tests.payroll.test_run_calculation import run_state as calc_state
from tests.payroll.test_run_finalize import (  # noqa: F401 (fin_world is a fixture)
    ATTESTATIONS,
    _claim,
    approve,
    approved_run,
    code_of,
    confirm_countries,
    fin_world,
    finalize,
    line,
    step_up,
    submit,
    ytd,
)
from tests.payroll.test_runs_lifecycle import (  # noqa: F401 (runs_world is a fixture)
    RUNS,
    current_period,
    force_status,
    new_run,
    periods_of,
    run_employees,
    runs_world,
    session_for,
)
from tests.payroll.test_snapshot_builder import emma as emma_of
from tests.payroll.test_snapshot_builder import lock, objects
from tests.payroll.test_wage_inputs import component, set_setting, sql

OFF_CYCLE = "/api/v1/payroll/off-cycle-runs"


def consumed_task_sources(task):
    return sorted(sql("SELECT run_id, source_key FROM payroll_line_sources WHERE source_type = 'TASK' AND consumed "
                      "AND source_id = :t", t=task))


def attest(app, w, legal_entity_id, reason):
    attestation = ok(w["acme"].post(ATTESTATIONS, json={"legal_entity_id": legal_entity_id,
                                                        "effective_from": "2020-01-01", "reason": reason}), 201)
    checker = login_as(app, w["acme"], create_employee(w["acme"], "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    return attestation


# ---------------------------------------------------------------------------
# 1. Per-assignee task payment (EC-TSK-*, EC-VAR-01)
# ---------------------------------------------------------------------------
def test_ec_tsk_ec_var_01_verify_two_pay_groups_open_together_finalize_in_reverse_order_and_retry(app, fin_world):
    """Emma (PG-IN, INR) 70 % and Gus (PG-AE, AED) 30 % share one task. Both runs are APPROVED before either
    finalizes; the AE run finalizes first. Each run pays only its own assignee's share; the task turns PAID with
    the IN run (the last share); a retry with the same key replays and a new key is refused (RUN_FINALIZED)
    without touching the shares."""
    from app.payroll.services import idempotency

    w = fin_world
    acme = w["acme"]
    emma, gus = w["emp"]["Emma"]["id"], w["gus"]["id"]
    ae_period = current_period(acme, w["ae_group"])
    assign_and_approve(acme, w["approver"], gus, w["version"], "1200000",
                       effective_from=date.fromisoformat(ae_period["start"]), currency="AED")
    attest(app, w, w["ae"], "Handled by our UAE provider")
    task = shared_task(w, [(emma, "70"), (gus, "30")])

    in_run = approved_run(w, prepared={emma: [line("BASIC", "50000.00"), task_line(task, "700.00")]})
    ae_run = approved_run(w, group=w["ae_group"], period=ae_period,
                          prepared={gus: [line("BASIC", "4000.00"), task_line(task, "300.00")]})

    finalize(acme, ae_run["id"], key="verify-ae")
    assert shares_of(task) == {emma: None, gus: ae_run["id"]}
    assert task_state(task) == ("APPROVED", None)                     # Emma's share is still open
    assert consumed_task_sources(task) == [(ae_run["id"], f"employee:{gus}")]

    finalize(acme, in_run["id"], key="verify-in")
    assert shares_of(task) == {emma: in_run["id"], gus: ae_run["id"]}
    assert task_state(task) == ("PAID", in_run["id"])                 # turned PAID with the last share
    assert consumed_task_sources(task) == sorted([(ae_run["id"], f"employee:{gus}"),
                                                  (in_run["id"], f"employee:{emma}")])

    replay = finalize(acme, in_run["id"], key="verify-in")            # same key: the stored response
    assert replay.headers.get(idempotency.REPLAY_HEADER) == "true"
    again = finalize(acme, in_run["id"], key="verify-in-2", expect=409)
    assert code_of(again) == "RUN_FINALIZED"
    assert shares_of(task) == {emma: in_run["id"], gus: ae_run["id"]}
    assert len(consumed_task_sources(task)) == 2


def test_ec_tsk_ec_var_01_verify_both_assignees_in_one_run_pay_both_shares_and_the_task_once(fin_world):
    w = fin_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    task = shared_task(w, [(emma, "60"), (ivan, "40")])
    run = approved_run(w, prepared={emma: [line("BASIC", "50000.00"), task_line(task, "120.00")],
                                    ivan: [line("BASIC", "50000.00"), task_line(task, "80.00")]})
    finalize(w["acme"], run["id"], key="verify-one-run")
    assert shares_of(task) == {emma: run["id"], ivan: run["id"]}
    assert task_state(task) == ("PAID", run["id"])
    assert consumed_task_sources(task) == sorted([(run["id"], f"employee:{emma}"), (run["id"], f"employee:{ivan}")])
    # One TASK_PAID audit per share plus one for the task turning PAID: 3 rows.
    assert sql("SELECT count(*) FROM audit_logs WHERE company_id = :c AND action = 'TASK_PAID' AND entity_type = 'task' "
               "AND entity_id = :t",
               c=w["cid"], t=str(task))[0][0] == 3


def test_ec_tsk_ec_var_01_verify_a_share_paid_elsewhere_rolls_back_the_whole_finalization(fin_world):
    """Ivan's share was already paid by another run (9001). A run that pays both shares is refused with 409
    TASK_SHARE_ALREADY_PAID and nothing of it sticks: Emma's share stays unpaid, no TASK source is consumed, the
    run stays APPROVED."""
    from app.payroll.services import tasks

    w = fin_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    task = shared_task(w, [(emma, "50"), (ivan, "50")])
    db = session_for(w["cid"])
    try:
        assert tasks.mark_task_shares_paid(db, w["cid"], [(task, ivan)], 9001) == 1
        db.commit()
    finally:
        db.close()
    run = approved_run(w, prepared={emma: [line("BASIC", "50000.00"), task_line(task, "100.00")],
                                    ivan: [line("BASIC", "50000.00"), task_line(task, "100.00")]})
    r = finalize(w["acme"], run["id"], key="verify-share-paid", expect=409)
    assert code_of(r) == "TASK_SHARE_ALREADY_PAID"
    assert shares_of(task) == {emma: None, ivan: 9001}
    assert task_state(task) == ("APPROVED", None)
    assert consumed_task_sources(task) == []
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] == "APPROVED"


def test_ec_tsk_ec_var_01_verify_real_snapshots_see_only_their_own_share_being_paid(runs_world):
    """Real snapshots of Emma and Ivan (one run) both freeze the shared task. Paying Emma's share elsewhere
    changes Emma's frozen task (REMOVED: never paid twice) and nothing of Ivan's; paying Ivan's share then
    changes Ivan's."""
    from app.payroll.services import tasks

    w = runs_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    task = shared_task(w, [(emma, "60"), (ivan, "40")])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(run["id"], "READY")
    stored = lock(w["cid"], run["id"], [emma, ivan]).snapshots
    force_status(run["id"], "REVIEW_REQUIRED")
    assert all(f"task:{task}" in s.source_hashes for s in stored.values())
    assert _detect(w, run["id"], full=True) == {}

    db = session_for(w["cid"])
    try:
        tasks.mark_task_shares_paid(db, w["cid"], [(task, emma)], 9001)
        db.commit()
    finally:
        db.close()
    changes = _detect(w, run["id"], full=True)
    assert changes == {emma: [{"source": f"task:{task}", "type": "task", "change": "REMOVED"}]}

    db = session_for(w["cid"])
    try:
        tasks.mark_task_shares_paid(db, w["cid"], [(task, ivan)], 9002)
        db.commit()
    finally:
        db.close()
    changes = _detect(w, run["id"], full=True)
    assert changes[ivan] == [{"source": f"task:{task}", "type": "task", "change": "REMOVED"}]


def test_ec_tsk_ec_var_01_verify_retro_late_task_pays_only_the_arrears_employees_share(runs_world):
    """A late task paid through Emma's arrears pays Emma's share only (retro._consume_paid_sources); Ivan's
    share stays open for his own run."""
    from app.payroll.services import retro

    w = runs_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    task = shared_task(w, [(emma, "50"), (ivan, "50")])
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    emma_row = run_employees(run["id"])[emma][2]
    proposal = SimpleNamespace(employee_id=emma, snapshot={"tasks": {"records": [{"task_id": f"task:{task}"}]}},
                               changed_sources=[{"type": "TASK"}])
    db = session_for(w["cid"])
    try:
        retro._consume_paid_sources(db, proposal, SimpleNamespace(company_id=w["cid"], id=run["id"]), emma_row)
        db.commit()
    finally:
        db.close()
    assert shares_of(task) == {emma: run["id"], ivan: None}     # Ivan was never paid for his half: still open
    assert task_state(task) == ("APPROVED", None)


# ---------------------------------------------------------------------------
# 2. Full change detection at the gates (EC-RUN-09)
# ---------------------------------------------------------------------------
def test_ec_run_09_verify_a_refused_finalization_stays_refused_on_a_retry_with_the_same_key(app, runs_world):
    """Real chain: calculate, submit, approve; then a YTD balance (not in the change log) appears. Finalize is
    refused (409 RUN_STALE, marker kept). The same Idempotency-Key retried is refused again by the stale
    blocker: no cached success, no second change-detection reason, nothing finalized or consumed."""
    w = runs_world
    acme, cid = w["acme"], w["cid"]
    emma = w["emp"]["Emma"]["id"]
    no_pack_country(w)
    confirm_countries(cid)
    set_setting(cid, "payroll.attendance.enabled", False)
    attest(app, w, w["le01"], "Statutory filings handled by our UK bureau")
    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert calc_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    assert calc_state(run["id"])[0] == "APPROVED"

    balance = _ytd(w, emma, "TOTAL:GROSS", "1234.0000")
    step_up(acme)
    first = finalize(acme, run["id"], key="verify-gate-retry", expect=409)
    assert code_of(first) == "RUN_STALE"
    second = finalize(acme, run["id"], key="verify-gate-retry", expect=409)
    assert code_of(second) == "RUN_STALE"
    stale, reasons = stale_of(run["id"])
    gate_reasons = [r for r in reasons if r.get("source") == "change_detection" and r.get("run_id") == run["id"]]
    assert stale and len(gate_reasons) == 1
    assert gate_reasons[0]["employee_ids"] == [emma]
    assert {"employee_id": emma, "source": f"ytd_balance:{balance}", "type": "ytd_balance",
            "change": "ADDED"} in gate_reasons[0]["changes"]
    assert sql("SELECT status_code FROM idempotency_keys WHERE company_id = :c AND key = 'verify-gate-retry'",
               c=cid) == [(None,)]
    assert sql("SELECT status, finalized_at FROM payroll_runs WHERE id = :r", r=run["id"]) == [("APPROVED", None)]
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=run["id"])[0][0] == 0


def test_ec_run_09_verify_the_attestation_filter_never_hides_a_real_change_made_with_it(app, runs_world):
    from app.payroll.services import change_detection

    w = runs_world
    emma = emma_of(w)
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(run["id"], "READY")
    lock(w["cid"], run["id"], [emma])
    force_status(run["id"], "APPROVED")
    attest(app, w, w["le01"], "Statutory filings by our external provider")
    balance = _ytd(w, emma, "TOTAL:NET", "77.0000")
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, run["id"])
        with pytest.raises(change_detection.RunMarkedStale) as info:
            change_detection.gate_full_check(db, found, "finalization")
        changes = info.value.summary["changes"][str(emma)]
        assert {c["type"] for c in changes} == {"ytd_balance"}           # the attestation is filtered, YTD is not
        assert {"source": f"ytd_balance:{balance}", "type": "ytd_balance", "change": "ADDED"} in changes
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. CLOSE audit without salary data
# ---------------------------------------------------------------------------
def test_close_verify_a_fully_held_finalized_run_closes_directly_and_audits_no_nets(fin_world):
    w = fin_world
    acme = w["acme"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    prepared = {e["id"]: [line("BASIC", "0.00")] for e in w["emp"].values()}    # anyone else: nothing payable
    prepared.update({emma: [line("BASIC", "61234.56")], ivan: [line("BASIC", "47890.12")]})
    run = approved_run(w, prepared=prepared)
    rows = run_employees(run["id"])
    for employee_id, reason in ((emma, "DISPUTE"), (ivan, "BANK_ISSUE")):
        r = acme.post(f"{RUNS}/{run['id']}/employees/{rows[employee_id][2]}/hold",
                      json={"hold_type": "PAYMENT", "reason_code": reason, "note": "Hold the transfer"})
        assert r.status_code == 200, r.text
    finalize(acme, run["id"], key="verify-close")
    closed = ok(acme.post(f"{RUNS}/{run['id']}/close", json={}))            # FINALIZED -> CLOSED: nothing payable
    assert closed["status"] == "CLOSED"
    expected = sorted([{"run_employee_id": rows[emma][2], "employee_id": emma, "reason_code": "DISPUTE"},
                       {"run_employee_id": rows[ivan][2], "employee_id": ivan, "reason_code": "BANK_ISSUE"}],
                      key=lambda h: h["run_employee_id"])
    assert closed["carried_holds"] == expected
    audits = sql("SELECT details FROM audit_logs WHERE company_id = :c AND entity_type = 'payroll_run' "
                 "AND entity_id = :r AND details::jsonb ? 'carried_holds'", c=w["cid"], r=str(run["id"]))
    assert len(audits) == 1
    details = audits[0][0]
    assert details["carried_holds"] == expected
    assert details["payment_summary"] == {"status": "FINALIZED", "payable_employees": 0, "open_items": None}
    text = str(details)
    assert "61234" not in text and "47890" not in text and "'net'" not in text


# ---------------------------------------------------------------------------
# 5. Off-cycle selection (EC-COR-06)
# ---------------------------------------------------------------------------
def _bonus_run(w, emma, codes):
    r = w["acme"].post(OFF_CYCLE, json={"kind": "BONUS", "reason": "Diwali bonus", "pay_group_id": w["group"],
                                        "pay_period_id": w["period"]["id"], "employee_ids": [emma],
                                        "component_codes": codes})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bonus_world(w):
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    component(w["cid"], "SPOT_BONUS")
    component(w["cid"], "SPOT_COMMISSION")


def test_ec_cor_06_verify_excluded_tasks_carry_no_hash_and_a_moved_input_is_still_seen(runs_world):
    w = runs_world
    emma = emma_of(w)
    _bonus_world(w)
    task = shared_task(w, [(emma, "100")])
    bonus = _wage_input(w, emma, "SPOT_BONUS", "2500.00")
    commission = _wage_input(w, emma, "SPOT_COMMISSION", "800.00")
    bonus_run = _bonus_run(w, emma, ["SPOT_BONUS"])
    force_status(bonus_run, "READY")
    [stored] = lock(w["cid"], bonus_run, [emma]).snapshots.values()
    force_status(bonus_run, "REVIEW_REQUIRED")
    assert not (stored.content.get("tasks") or {}).get("records")    # the task is left out of the bonus run
    assert not any(k.startswith(("task:", "rate_card:")) for k in stored.source_hashes)
    assert f"wage_input:{bonus}" in stored.source_hashes and f"wage_input:{commission}" not in stored.source_hashes

    sql("UPDATE tasks SET accepted_quantity = 1 WHERE id = :t", t=task)          # excluded task changed: ignored
    assert _detect(w, bonus_run, full=True) == {}
    sql("UPDATE wage_inputs SET component_code = 'SPOT_BONUS' WHERE id = :i", i=commission)   # now selected
    changes = _detect(w, bonus_run, full=True)
    assert changes == {emma: [{"source": f"wage_input:{commission}", "type": "wage_input", "change": "ADDED"}]}


def test_ec_cor_06_verify_the_regular_runs_pass_does_not_mark_the_bonus_run_stale_for_an_excluded_input(
        runs_world):
    from app.payroll.services import change_detection

    w = runs_world
    emma = emma_of(w)
    _bonus_world(w)
    _wage_input(w, emma, "SPOT_BONUS", "2500.00")
    _wage_input(w, emma, "SPOT_COMMISSION", "800.00")
    bonus_run = _bonus_run(w, emma, ["SPOT_BONUS"])
    force_status(bonus_run, "READY")
    lock(w["cid"], bonus_run, [emma])
    force_status(bonus_run, "REVIEW_REQUIRED")
    regular = new_run(w["acme"], w["group"], w["period"]["id"])["id"]
    force_status(regular, "READY")
    lock(w["cid"], regular, [emma])
    force_status(regular, "REVIEW_REQUIRED")
    added = _wage_input(w, emma, "SPOT_COMMISSION", "300.00")       # excluded by the bonus run, paid by the regular

    if _detect(w, bonus_run, full=True) != {}:
        raise RuntimeError("the bonus run's own detection sees the excluded input")
    if {c["source"] for c in _detect(w, regular, full=True).get(emma, [])} != {f"wage_input:{added}"}:
        raise RuntimeError("the regular run does not see the new input")
    db = session_for(w["cid"])
    try:
        found, _ = objects(db, regular)
        if change_detection.match_run(db, found, full=True)["marked"] is not True:
            raise RuntimeError("the regular run's pass marked nothing")
        db.commit()
    finally:
        db.close()
    if stale_of(regular)[0] is not True:
        raise RuntimeError("the regular run was not marked stale")
    assert stale_of(bonus_run)[0] is False



# ===========================================================================
# Second skeptic pass (key p4-verify): paths the first pass did not walk
# ===========================================================================
# 1. EC-TSK-* / EC-VAR-01 -----------------------------------------------------
def test_ec_tsk_ec_var_01_verify_a_share_paid_meanwhile_turns_the_task_paid_with_the_last_share(runs_world):
    """Two finalizations of different pay groups race: run B (Ivan's share) has already read the shared task in
    its session (its full change-detection gate rebuilds Ivan's snapshot, which loads the task and its assignees)
    when run A pays Emma's share and commits. B then pays Ivan's share, the last one: the task must turn PAID
    with B (9002), not stay APPROVED with every share paid (which no run would ever close and which the invoice
    path reads as payable)."""
    from app.payroll.services import tasks

    w = runs_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    task = shared_task(w, [(emma, "50"), (ivan, "50")])
    run_b = session_for(w["cid"])
    try:
        seen = tasks.approved_tasks(run_b, w["cid"], ivan, start, end, "APPROVAL_DATE")   # B's gate reads it
        assert [t["id"] for t in seen] == [task]
        run_a = session_for(w["cid"])
        try:
            assert tasks.mark_task_shares_paid(run_a, w["cid"], [(task, emma)], 9001) == 1
            run_a.commit()
        finally:
            run_a.close()
        assert tasks.mark_task_shares_paid(run_b, w["cid"], [(task, ivan)], 9002) == 1
        run_b.commit()
    finally:
        run_b.close()
    assert shares_of(task) == {emma: 9001, ivan: 9002}
    assert task_state(task) == ("PAID", 9002)


def test_ec_tsk_ec_var_01_verify_a_share_paid_meanwhile_is_never_paid_again_by_a_stale_session(runs_world):
    """The same race on ONE share (a retro arrears line and Emma's regular run): the session that read the task
    before the other paid Emma's share must be refused (409 TASK_SHARE_ALREADY_PAID), never overwrite 9001."""
    from app.core.errors import AppError
    from app.payroll.services import tasks

    w = runs_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    start, end = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    task = shared_task(w, [(emma, "50"), (ivan, "50")])
    late = session_for(w["cid"])
    try:
        assert [t["id"] for t in tasks.approved_tasks(late, w["cid"], emma, start, end, "APPROVAL_DATE")] == [task]
        first = session_for(w["cid"])
        try:
            tasks.mark_task_shares_paid(first, w["cid"], [(task, emma)], 9001)
            first.commit()
        finally:
            first.close()
        with pytest.raises(AppError) as info:
            tasks.mark_task_shares_paid(late, w["cid"], [(task, emma)], 9002)
        assert info.value.status_code == 409 and info.value.code == "TASK_SHARE_ALREADY_PAID"
        late.rollback()
    finally:
        late.close()
    assert shares_of(task) == {emma: 9001, ivan: None}
    assert task_state(task) == ("APPROVED", None)


def _migration_0028():
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_task_assignee_paid.py"
    spec = importlib.util.spec_from_file_location("migration_0028_task_assignee_paid", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ec_tsk_ec_var_01_verify_migration_0028_backfills_paid_shares_and_rekeys_open_task_sources(fin_world,
                                                                                                    monkeypatch):
    """The 0028 data migration on the state the OLD code left: task T1 (Emma 60 / Ivan 40) was paid whole by
    run R1 (tasks.paid_in_run_id = R1, its consumed source keyed ''), task T2 (Emma 50 / Ivan 50) sits in the
    APPROVED run R2 with an unconsumed source keyed ''. By hand: T1's shares become {Emma: R1 (her consumed
    source), Ivan: R1 (the task said paid: fail closed)}; T2's shares stay unpaid; R2's source is re-keyed
    'employee:<Emma>' while R1's consumed source keeps ''. R2 then finalizes Emma's share of T2 only."""
    w = fin_world
    acme = w["acme"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    t1 = shared_task(w, [(emma, "60"), (ivan, "40")])
    t2 = shared_task(w, [(emma, "50"), (ivan, "50")])
    r1 = approved_run(w, prepared={emma: [line("BASIC", "50000.00"), task_line(t1, "120.00")]})
    # What the old code wrote: the TASK source consumed under the key '' (written before R1 is finalized: the
    # finalized-run trigger rightly refuses any later change), the whole task PAID by R1, no share markers.
    line_id, row_id = sql("SELECT id, run_employee_id FROM payroll_lines WHERE run_id = :r "
                          "AND component_code = 'TASK_EARNING'", r=r1["id"])[0]
    sql("INSERT INTO payroll_line_sources (company_id, line_id, run_id, run_employee_id, source_type, source_id, "
        "source_key, consumed, consumed_at, created_at, updated_at) VALUES (:c, :l, :r, :e, 'TASK', :t, '', true, "
        "now(), now(), now())", c=w["cid"], l=line_id, r=r1["id"], e=row_id, t=t1)
    finalize(acme, r1["id"], key="verify-0028-r1")
    assert sql("SELECT source_key, consumed FROM payroll_line_sources WHERE run_id = :r AND source_type = 'TASK'",
               r=r1["id"]) == [("", True)]
    sql("UPDATE task_assignees SET paid_in_run_id = NULL WHERE task_id = :t", t=t1)
    sql("UPDATE tasks SET status = 'PAID', paid_in_run_id = :r WHERE id = :t", r=r1["id"], t=t1)

    following = min((p for p in periods_of(acme, w["group"]) if p["start"] > w["period"]["end"]),
                    key=lambda p: p["start"])
    r2 = approved_run(w, period=following, prepared={emma: [line("BASIC", "50000.00"), task_line(t2, "100.00")]})
    task_line_id, row_id = sql("SELECT id, run_employee_id FROM payroll_lines WHERE run_id = :r "
                               "AND component_code = 'TASK_EARNING'", r=r2["id"])[0]
    sql("INSERT INTO payroll_line_sources (company_id, line_id, run_id, run_employee_id, source_type, source_id, "
        "source_key, consumed, created_at, updated_at) VALUES (:c, :l, :r, :e, 'TASK', :t, '', false, now(), now())",
        c=w["cid"], l=task_line_id, r=r2["id"], e=row_id, t=t2)

    migration = _migration_0028()
    monkeypatch.setattr(migration, "op", SimpleNamespace(add_column=lambda *a, **k: None,
                                                         create_index=lambda *a, **k: None,
                                                         execute=lambda statement: sql(statement)))
    migration.upgrade()

    assert shares_of(t1) == {emma: r1["id"], ivan: r1["id"]}
    assert task_state(t1) == ("PAID", r1["id"])
    assert shares_of(t2) == {emma: None, ivan: None}
    assert sql("SELECT source_key FROM payroll_line_sources WHERE run_id = :r AND source_type = 'TASK'",
               r=r1["id"]) == [("",)]
    assert sql("SELECT source_key, consumed FROM payroll_line_sources WHERE run_id = :r AND source_type = 'TASK'",
               r=r2["id"]) == [(f"employee:{emma}", False)]

    finalize(acme, r2["id"], key="verify-0028-r2")
    assert shares_of(t2) == {emma: r2["id"], ivan: None}
    assert task_state(t2) == ("APPROVED", None)
    assert consumed_task_sources(t2) == [(r2["id"], f"employee:{emma}")]


# 2. EC-RUN-09 ----------------------------------------------------------------
def test_ec_run_09_verify_a_claim_raised_after_approval_is_caught_by_the_finalize_gate(app, fin_world):
    """Real chain. By hand (CTC 12,00,000, no pack, I-15 attestation): Emma's gross is BASIC 50,000.00 + HRA
    20,000.00 + FIXED_ALLOWANCE 30,000.00 = 1,00,000.00; her approved travel claim 300.00 makes net 1,00,300.00.
    Locking schedules the claim, which is not a change (the submission gate lets the run through). After the
    approval the claim is raised to 900.00 (claims are not in the source change log): finalize is refused with
    409 RUN_STALE naming claim:<id> UPDATED and nothing is paid. Reopened and recalculated: net
    1,00,000.00 + 900.00 = 1,00,900.00, the run finalizes and the claim is PAID once."""
    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma = w["emp"]["Emma"]["id"]
    no_pack_country(w)
    set_setting(cid, "payroll.attendance.enabled", False)
    claim = _claim(w, emma, "300.00")
    attest(app, w, w["le01"], "Statutory filings by our external provider")
    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert calc_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    emma_row = rows_of(run["id"])[emma][0]
    assert lines_of(emma_row)["REIMBURSEMENT"] == (Decimal("300.00"), "REIMBURSEMENT")
    assert rows_of(run["id"])[emma][4] == Decimal("100300")
    assert sql("SELECT status FROM reimbursement_claims WHERE id = :c", c=claim)[0][0] == "SCHEDULED"
    submit(acme, run["id"])
    approve(w["approver"], run["id"])

    sql("UPDATE reimbursement_claims SET amount = 900, payout_amount = 900, approved_amount = 900, "
        "row_version = row_version + 1 WHERE id = :c", c=claim)
    step_up(acme)
    r = finalize(acme, run["id"], key="verify-claim-1", expect=409)
    assert code_of(r) == "RUN_STALE"
    stale, reasons = stale_of(run["id"])
    assert stale and reasons[-1]["employee_ids"] == [emma]
    assert {"employee_id": emma, "source": f"claim:{claim}", "type": "claim", "change": "UPDATED"} \
        in reasons[-1]["changes"]
    assert calc_state(run["id"])[0] == "APPROVED"
    assert sql("SELECT status FROM reimbursement_claims WHERE id = :c", c=claim)[0][0] == "SCHEDULED"
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=run["id"])[0][0] == 0
    assert ytd(cid, emma, "TOTAL:NET") is None

    ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Claim corrected after approval",
                                                       "target": "REVIEW_REQUIRED"}))
    calculate_job(acme, run["id"], employee_ids=[emma])
    assert calc_state(run["id"])[0] == "REVIEW_REQUIRED", findings(run["id"])
    assert lines_of(emma_row)["REIMBURSEMENT"] == (Decimal("900.00"), "REIMBURSEMENT")
    assert rows_of(run["id"])[emma][4] == Decimal("100900")
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    assert finalize(acme, run["id"], key="verify-claim-2").json()["status"] == "FINALIZED"
    assert sql("SELECT status FROM reimbursement_claims WHERE id = :c", c=claim)[0][0] == "PAID"
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed AND source_type = 'CLAIM'",
               r=run["id"])[0][0] == 1
    assert ytd(cid, emma, "TOTAL:NET") == Decimal("100900")


# 3. CLOSE audit ----------------------------------------------------------------
def test_close_verify_extra_hold_fields_never_reach_the_close_audit(fin_world):
    """The hold JSON may carry more than a reason (a note, an amount, an account): the CLOSE audit keeps the
    three named fields only (an allow-list, not a deny-list of 'net')."""
    w = fin_world
    acme = w["acme"]
    ivan = w["emp"]["Ivan"]["id"]
    run = approved_run(w, prepared={})                                  # everyone BASIC 50,000.00
    ivan_row = run_employees(run["id"])[ivan][2]
    r = acme.post(f"{RUNS}/{run['id']}/employees/{ivan_row}/hold",
                  json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE", "note": "Account closed"})
    assert r.status_code == 200, r.text
    sql("UPDATE payroll_run_employees SET hold = hold || CAST(:x AS jsonb) WHERE id = :r", r=ivan_row,
        x=json.dumps({"net": "50000.00", "amount": "50000.00", "account_number": "XX99887766"}))
    finalize(acme, run["id"], key="verify-close-extra")
    force_status(run["id"], "RECONCILED")
    closed = ok(acme.post(f"{RUNS}/{run['id']}/close", json={}))
    assert closed["carried_holds"] == [{"run_employee_id": ivan_row, "employee_id": ivan, "reason_code": "BANK_ISSUE"}]
    rows = sql("SELECT details FROM audit_logs WHERE company_id = :c AND entity_type = 'payroll_run' "
               "AND entity_id = :r AND details::jsonb ? 'carried_holds'", c=w["cid"], r=str(run["id"]))
    assert len(rows) == 1
    details = rows[0][0]
    assert details["carried_holds"] == [{"run_employee_id": ivan_row, "employee_id": ivan,
                                         "reason_code": "BANK_ISSUE"}]
    text = json.dumps(details)
    assert "50000" not in text and "XX99887766" not in text and "Account closed" not in text


# 5. EC-COR-06 ------------------------------------------------------------------
def test_ec_cor_06_verify_claims_left_out_of_a_bonus_run_are_neither_hashed_nor_scheduled(runs_world):
    """A BONUS run selecting SPOT_BONUS leaves Emma's approved claim out: no claim hash, the claim is not
    scheduled by the bonus run (it stays APPROVED for the regular run), and raising it or adding another claim
    never marks the bonus run stale. The selected bonus changed (2,500.00 -> 2,600.00) still does (UPDATED)."""
    w = runs_world
    emma = emma_of(w)
    _bonus_world(w)
    claim = _claim(w, emma, "300.00")
    bonus = _wage_input(w, emma, "SPOT_BONUS", "2500.00")
    bonus_run = _bonus_run(w, emma, ["SPOT_BONUS"])
    force_status(bonus_run, "READY")
    [stored] = lock(w["cid"], bonus_run, [emma]).snapshots.values()
    force_status(bonus_run, "REVIEW_REQUIRED")
    assert stored.content.get("reimbursements") == []
    assert f"claim:{claim}" not in stored.source_hashes and f"wage_input:{bonus}" in stored.source_hashes
    assert sql("SELECT status, scheduled_ref FROM reimbursement_claims WHERE id = :c", c=claim)[0] == ("APPROVED", None)

    sql("UPDATE reimbursement_claims SET amount = 900, payout_amount = 900, approved_amount = 900, "
        "row_version = row_version + 1 WHERE id = :c", c=claim)
    _claim(w, emma, "450.00")
    assert _detect(w, bonus_run, full=True) == {}
    sql("UPDATE wage_inputs SET amount = 2600, row_version = row_version + 1 WHERE id = :i", i=bonus)
    assert _detect(w, bonus_run, full=True) == {
        emma: [{"source": f"wage_input:{bonus}", "type": "wage_input", "change": "UPDATED"}]}


# ===========================================================================
# Third skeptic pass (key p4-verify)
# ===========================================================================
# 1. EC-TSK-* / EC-VAR-01 -----------------------------------------------------
def test_ec_tsk_ec_var_01_verify_task_objects_held_by_the_paying_session_are_reread_under_the_lock(runs_world):
    """The paying session still HOLDS the Task objects it loaded before another run paid Emma's shares (the
    finalize gate's snapshot rebuild loads them; nothing guarantees they are garbage collected before the
    lock). Under the task lock the shares must be re-read: by hand, task 2 (Emma 70 / Ivan 30) gets Ivan's
    share from run 9002, the last one, so it turns PAID with 9002; task 1 (Emma 50 / Ivan 50) is refused for
    Emma (409 TASK_SHARE_ALREADY_PAID) and keeps Emma = 9001, Ivan unpaid. Before the fix the stale identity
    map left task 2 APPROVED with every share paid and silently overwrote Emma's 9001 with 9002."""
    from sqlalchemy.orm import selectinload

    from app.core.errors import AppError
    from app.models.work_items import Task
    from app.payroll.services import tasks

    w = runs_world
    cid = w["cid"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    t1 = shared_task(w, [(emma, "50"), (ivan, "50")])
    t2 = shared_task(w, [(emma, "70"), (ivan, "30")])
    late = session_for(cid)
    try:
        held = (late.query(Task).options(selectinload(Task.assignees)).filter(Task.id.in_([t1, t2]))
                .order_by(Task.id).all())
        assert [[a.paid_in_run_id for a in t.assignees] for t in held] == [[None, None], [None, None]]
        first = session_for(cid)
        try:
            assert tasks.mark_task_shares_paid(first, cid, [(t1, emma), (t2, emma)], 9001) == 2
            first.commit()
        finally:
            first.close()
        assert tasks.mark_task_shares_paid(late, cid, [(t2, ivan)], 9002) == 1
        late.commit()
        with pytest.raises(AppError) as info:
            tasks.mark_task_shares_paid(late, cid, [(t1, emma)], 9002)
        assert info.value.status_code == 409 and info.value.code == "TASK_SHARE_ALREADY_PAID"
        late.rollback()
        assert len(held) == 2                                   # the stale objects were held throughout
    finally:
        late.close()
    assert shares_of(t2) == {emma: 9001, ivan: 9002}
    assert task_state(t2) == ("PAID", 9002)
    assert shares_of(t1) == {emma: 9001, ivan: None}
    assert task_state(t1) == ("APPROVED", None)


def test_ec_tsk_ec_var_01_verify_a_task_line_on_a_non_assignees_payslip_never_pays_the_assignees_share(fin_world):
    """A TASK_EARNING line of 250.00 lands on Ivan's payslip for a task whose only assignee is Emma (a manual or
    mis-attributed line). Finalization is refused with 409 TASK_SHARE_NOT_FOUND and nothing sticks: Emma's share
    stays unpaid (her own run still pays it), no TASK source is consumed, the run stays APPROVED. The old
    whole-task marker would have marked the task PAID through Ivan's run."""
    w = fin_world
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    task = shared_task(w, [(emma, "100")])
    run = approved_run(w, prepared={ivan: [line("BASIC", "50000.00"), task_line(task, "250.00")]})
    r = finalize(w["acme"], run["id"], key="verify-not-an-assignee", expect=409)
    assert code_of(r) == "TASK_SHARE_NOT_FOUND"
    assert shares_of(task) == {emma: None}
    assert task_state(task) == ("APPROVED", None)
    assert consumed_task_sources(task) == []
    assert sql("SELECT status FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] == "APPROVED"


# 2. EC-RUN-09 (with EC-COR-06) ---------------------------------------------------
def test_ec_run_09_ec_cor_06_verify_the_finalize_gate_of_a_bonus_run_ignores_only_excluded_inputs(runs_world):
    """An APPROVED BONUS run selecting SPOT_BONUS (2,500.00). After approval the excluded commission is raised
    800.00 -> 950.00 and a 300.00 claim appears (REIMBURSEMENT not selected): the full finalize gate passes
    (checked, nothing marked). Raising the selected bonus to 2,600.00 trips it with exactly one change,
    wage_input:<bonus> UPDATED; the service never commits, so the run is not stale after the rollback."""
    from app.payroll.services import change_detection

    w = runs_world
    cid = w["cid"]
    emma = emma_of(w)
    _bonus_world(w)
    bonus = _wage_input(w, emma, "SPOT_BONUS", "2500.00")
    commission = _wage_input(w, emma, "SPOT_COMMISSION", "800.00")
    bonus_run = _bonus_run(w, emma, ["SPOT_BONUS"])
    force_status(bonus_run, "READY")
    lock(cid, bonus_run, [emma])
    force_status(bonus_run, "APPROVED")

    sql("UPDATE wage_inputs SET amount = 950, row_version = row_version + 1 WHERE id = :i", i=commission)
    _claim(w, emma, "300.00")
    db = session_for(cid)
    try:
        found, _ = objects(db, bonus_run)
        outcome = change_detection.gate_full_check(db, found, "finalization")
        assert outcome["checked"] is True and outcome["marked"] is False and outcome["changes"] == {}
        db.rollback()
    finally:
        db.close()
    assert stale_of(bonus_run)[0] is False

    sql("UPDATE wage_inputs SET amount = 2600, row_version = row_version + 1 WHERE id = :i", i=bonus)
    db = session_for(cid)
    try:
        found, _ = objects(db, bonus_run)
        with pytest.raises(change_detection.RunMarkedStale) as info:
            change_detection.gate_full_check(db, found, "finalization")
        assert info.value.status_code == 409 and info.value.code == "RUN_STALE"
        assert info.value.summary["changes"] == {
            str(emma): [{"source": f"wage_input:{bonus}", "type": "wage_input", "change": "UPDATED"}]}
        db.rollback()
    finally:
        db.close()
    assert stale_of(bonus_run)[0] is False


# 3. CLOSE audit ----------------------------------------------------------------
def test_close_verify_a_replaced_payment_summary_hook_never_puts_amounts_in_the_close_audit(fin_world, monkeypatch):
    """``finalization.payment_summary`` is a hook the payments milestone may replace. A replacement that also
    reports amounts (open 50,000.00, paid 12,34,567.89) must not carry them into the CLOSE audit: the audit
    keeps status, payable_employees and open_items only (RECONCILED, 0, 0)."""
    from app.payroll.services import finalization

    w = fin_world
    acme = w["acme"]
    ivan = w["emp"]["Ivan"]["id"]
    run = approved_run(w, prepared={})
    ivan_row = run_employees(run["id"])[ivan][2]
    r = acme.post(f"{RUNS}/{run['id']}/employees/{ivan_row}/hold",
                  json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE", "note": "Hold the transfer"})
    assert r.status_code == 200, r.text
    finalize(acme, run["id"], key="verify-close-hook")
    force_status(run["id"], "RECONCILED")
    monkeypatch.setattr(finalization, "payment_summary", lambda db, found: {
        "status": found.status, "payable_employees": 0, "open_items": 0, "open_amount": "50000.00",
        "paid_total": "1234567.89", "currency": "INR"})
    closed = ok(acme.post(f"{RUNS}/{run['id']}/close", json={}))
    assert closed["status"] == "CLOSED"
    rows = sql("SELECT details FROM audit_logs WHERE company_id = :c AND entity_type = 'payroll_run' "
               "AND entity_id = :r AND details::jsonb ? 'carried_holds'", c=w["cid"], r=str(run["id"]))
    assert len(rows) == 1
    details = rows[0][0]
    assert details["payment_summary"] == {"status": "RECONCILED", "payable_employees": 0, "open_items": 0}
    assert details["carried_holds"] == [{"run_employee_id": ivan_row, "employee_id": ivan,
                                         "reason_code": "BANK_ISSUE"}]
    text = json.dumps(details)
    assert "50000" not in text and "1234567" not in text


# 5. EC-COR-06 ------------------------------------------------------------------
def test_ec_cor_06_verify_a_selected_time_pay_input_keeps_its_hash_and_is_still_watched(runs_world):
    """Fail closed in the other direction: a BONUS run selecting SPOT_BONUS and TASK_EARNING keeps Emma's task
    (and its hash) while the excluded commission has none. Raising the commission 800.00 -> 900.00 is ignored;
    accepting 1 of the task's 2 units marks exactly task:<id> UPDATED."""
    w = runs_world
    cid = w["cid"]
    emma = emma_of(w)
    _bonus_world(w)
    sql("UPDATE pay_component_versions v SET effective_from = DATE '2020-01-01' FROM pay_components p "
        "WHERE p.company_id = v.company_id AND p.id = v.component_id AND p.company_id = :c "
        "AND p.code = 'TASK_EARNING' AND v.version = 1", c=cid)
    task = shared_task(w, [(emma, "100")])
    commission = _wage_input(w, emma, "SPOT_COMMISSION", "800.00")
    bonus_run = _bonus_run(w, emma, ["SPOT_BONUS", "TASK_EARNING"])
    force_status(bonus_run, "READY")
    [stored] = lock(cid, bonus_run, [emma]).snapshots.values()
    force_status(bonus_run, "REVIEW_REQUIRED")
    assert [r["task_id"] for r in stored.content["tasks"]["records"]] == [f"task:{task}"]
    assert f"task:{task}" in stored.source_hashes
    assert f"wage_input:{commission}" not in stored.source_hashes

    sql("UPDATE wage_inputs SET amount = 900, row_version = row_version + 1 WHERE id = :i", i=commission)
    assert _detect(w, bonus_run, full=True) == {}
    sql("UPDATE tasks SET accepted_quantity = 1 WHERE id = :t", t=task)
    assert _detect(w, bonus_run, full=True) == {
        emma: [{"source": f"task:{task}", "type": "task", "change": "UPDATED"}]}
