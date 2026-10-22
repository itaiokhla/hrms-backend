"""Submission and approval of pay runs: the chain, separation of duties, self-pay, concurrency, reject and
rework, reopen and every forbidden transition (3.5 §4.1, §5.6, §5.7, §11, §12; 3.9 §4 Runs).

EC-RUN-05, EC-RUN-06, EC-RUN-07, EC-RUN-08, EC-RUN-09, EC-RUN-16, EC-RUN-17, EC-SEC-01, EC-SEC-02, EC-SEC-06.
"""

import threading
from datetime import date

import pytest
from sqlalchemy import text

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import ok
from tests.payroll.test_run_finalize import (  # noqa: F401  (fixtures)
    approve,
    approved_run,
    code_of,
    fin_world,
    finalize,
    prepare,
    reject,
    run_state,
    submit,
    user_id_of,
)
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    force_status,
    new_run,
    run_employees,
    runs_world,
)
from tests.payroll.test_wage_inputs import audit_actions, role, set_setting, sql, tenant, user_with_role

APPROVALS = f"{PAY}/approvals"


def reviewed_run(w, **results):
    """A run in REVIEW_REQUIRED with service-made results (every included employee BASIC 50,000.00)."""
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    prepare(w["acme"], run["id"], results or None)
    assert run_state(run["id"])["status"] == "REVIEW_REQUIRED"
    return run


def decisions(run_id):
    return sql("SELECT step_order, approver_user_id, decision, acted_as, run_employee_id, cleared_at IS NOT NULL "
               "FROM payroll_approvals WHERE run_id = :r ORDER BY id", r=run_id)


def request_status(run_id):
    rows = sql("SELECT status FROM approval_requests WHERE module = 'payroll_run' AND subject_id = :r ORDER BY id",
               r=run_id)
    return [s for (s,) in rows]


def add_finding(w, run_id, code, severity, *, resolutions=("WAIVE",), waivable=True, employee="Emma"):
    employee_id = w["emp"][employee]["id"]
    row = run_employees(run_id)[employee_id][2]
    return sql("INSERT INTO payroll_exceptions (company_id, run_id, run_employee_id, employee_id, code, severity, "
               "source, message, details, resolutions, waivable, blocks_finalization, dedupe_key, status, row_version, "
               "created_at, updated_at) VALUES (:c, :r, :re, :e, :code, :s, 'VALIDATION', 'Finding', '{}', "
               "CAST(:res AS jsonb), :wv, false, :k, 'OPEN', 1, now(), now()) RETURNING id", c=w["cid"], r=run_id,
               re=row, e=employee_id, code=code, s=severity, res=str(list(resolutions)).replace("'", '"'),
               wv=waivable, k=f"TEST:{code}")[0][0]


# ---------------------------------------------------------------------------
# Submit (3.5 §5.6)
# ---------------------------------------------------------------------------
def test_submit_needs_review_right_no_blocking_exception_and_acknowledged_variance(fin_world):
    w = fin_world
    run = reviewed_run(w)
    assert w["approver"].post(f"{RUNS}/{run['id']}/submit", json={}).status_code == 403  # no payroll:run:review
    assert w["beta"]["client"].post(f"{RUNS}/{run['id']}/submit", json={}).status_code == 404
    assert w["cleo"].post(f"{RUNS}/{run['id']}/submit", json={}).status_code == 404  # AE01 clerk
    blocking = add_finding(w, run["id"], "NEGATIVE_NET", "BLOCKING", resolutions=("FIX",), waivable=False)
    r = w["processor"].post(f"{RUNS}/{run['id']}/submit", json={})
    assert r.status_code == 409 and code_of(r) == "BLOCKING_EXCEPTIONS_OPEN", r.text
    sql("UPDATE payroll_exceptions SET status = 'FIXED', resolved_at = now() WHERE id = :e", e=blocking)
    variance = add_finding(w, run["id"], "NET_VARIANCE", "WARNING")
    r = w["processor"].post(f"{RUNS}/{run['id']}/submit", json={})
    assert r.status_code == 409 and code_of(r) == "VARIANCE_NOT_ACKNOWLEDGED", r.text
    ok(w["processor"].post(f"{PAY}/exceptions/{variance}/waive", json={"reason": "Annual increment, checked"}))
    r = w["processor"].post(f"{RUNS}/{run['id']}/submit", json={"comment": "Ready", "status": "APPROVED"})
    assert r.status_code == 422  # strict body: no server-owned fields (EC-SEC-06)
    submitted = submit(w["processor"], run["id"], comment="September looks right")
    assert submitted["status"] == "APPROVAL_PENDING" and submitted["approval_request_id"]
    context = sql("SELECT requested_by_user_id, context FROM approval_requests WHERE id = :i",
                  i=submitted["approval_request_id"])[0]
    assert context[0] == user_id_of(w["processor"])
    assert context[1]["legal_entity_id"] == w["le01"] and context[1]["separation_of_duties"] is True
    assert user_id_of(w["acme"]) in context[1]["maker_user_ids"]  # the founder calculated it
    assert "RUN_SUBMITTED" in audit_actions(w["cid"], "payroll_run", run["id"])
    r = w["processor"].post(f"{RUNS}/{run['id']}/submit", json={})
    assert r.status_code == 409 and code_of(r) == "INVALID_TRANSITION"


# ---------------------------------------------------------------------------
# Separation of duties (EC-RUN-05)
# ---------------------------------------------------------------------------
def _preparer_approver(app, w):
    """Sam may calculate, review and approve (COMPANY)."""
    codes = ("payroll:run:read", "payroll:run:create", "payroll:run:calculate", "payroll:run:review",
             "payroll:run:approve")
    sam_role = role(w["acme"], "Run all-rounder", "COMPANY", codes)
    sam = user_with_role(app, w["acme"], create_employee(w["acme"], "Sam", hire_date="2024-01-15"), sam_role)
    sam.company_id = w["cid"]
    return sam


def test_ec_run_05_separation_of_duties_on_blocks_the_preparer(app, fin_world):
    w = fin_world
    sam = _preparer_approver(app, w)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    prepare(sam, run["id"])           # Sam calculates
    submit(w["acme"], run["id"])      # the founder submits
    r = approve(sam, run["id"], expect=403)
    assert code_of(r) == "SEPARATION_OF_DUTIES"
    r = reject(sam, run["id"], expect=403)
    assert code_of(r) == "SEPARATION_OF_DUTIES"
    # The generic approvals inbox enforces the same (maker_user_ids).
    request_id = sql("SELECT approval_request_id FROM payroll_runs WHERE id = :r", r=run["id"])[0][0]
    assert sam.post(f"{APPROVALS}/{request_id}/approve", json={}).status_code == 403
    assert run_state(run["id"])["status"] == "APPROVAL_PENDING"
    approve(w["approver"], run["id"])
    assert run_state(run["id"])["status"] == "APPROVED"


def test_ec_run_05_separation_of_duties_off_lets_the_preparer_approve_the_run(app, fin_world):
    w = fin_world
    set_setting(w["cid"], "payroll.approval.separation_of_duties", False)
    sam = _preparer_approver(app, w)
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    prepare(sam, run["id"])
    submit(sam, run["id"])
    requested_by = sql("SELECT requested_by_user_id FROM approval_requests WHERE subject_id = :r "
                       "AND module = 'payroll_run'", r=run["id"])[0][0]
    assert requested_by is None  # raised by the system: the submitter is not barred
    result = approve(sam, run["id"])
    assert result["status"] == "APPROVED" and result["approval_request"]["status"] == "APPROVED"
    assert decisions(run["id"]) == [(1, sam.user_id, "APPROVED", "approver", None, False)]


# ---------------------------------------------------------------------------
# Self-pay (EC-RUN-06)
# ---------------------------------------------------------------------------
def test_ec_run_06_an_approvers_own_line_needs_another_approver(fin_world):
    from tests.payroll.test_wage_inputs import place

    w = fin_world
    ava = w["approver"]
    place(ava.employee["id"], w["le01"], w["group"])
    assign_and_approve(w["acme"], w["bob"], ava.employee["id"], w["version"], "1500000",
                       effective_from=date.fromisoformat(w["period"]["start"]))
    run = reviewed_run(w)
    ava_row = run_employees(run["id"])[ava.employee["id"]][2]
    emma_row = run_employees(run["id"])[w["emp"]["Emma"]["id"]][2]
    submit(w["acme"], run["id"])
    result = approve(ava, run["id"])   # Ava approves the run: the rest proceeds ...
    assert result["status"] == "APPROVED" and result["self_line_pending"] == [ava_row]
    pending = ok(w["acme"].get(f"{RUNS}/{run['id']}/approvals"))["self_lines_pending"]
    assert [p["run_employee_id"] for p in pending] == [ava_row]
    r = finalize(w["acme"], run["id"], expect=409)        # ... but her own line waits for another approver
    assert code_of(r) == "SELF_LINE_APPROVAL_PENDING"
    r = ava.post(f"{RUNS}/{run['id']}/employees/{ava_row}/approve-self-line", json={})
    assert r.status_code == 403 and code_of(r) == "SELF_PAY_APPROVAL"
    r = w["bob"].post(f"{RUNS}/{run['id']}/employees/{emma_row}/approve-self-line", json={})
    assert r.status_code == 409 and code_of(r) == "SELF_LINE_NOT_REQUIRED"
    assert w["processor"].post(f"{RUNS}/{run['id']}/employees/{ava_row}/approve-self-line", json={}).status_code == 403
    # Another tenant's founder holds no payroll:run:approve: refused before any lookup (function level).
    assert w["beta"]["client"].post(f"{RUNS}/{run['id']}/employees/{ava_row}/approve-self-line",
                                    json={}).status_code == 403
    line = ok(w["bob"].post(f"{RUNS}/{run['id']}/employees/{ava_row}/approve-self-line",
                            json={"comment": "Checked against her contract"}))
    assert line["self_line_approved_by_user_id"] == w["bob"].user_id
    again = ok(w["bob"].post(f"{RUNS}/{run['id']}/employees/{ava_row}/approve-self-line", json={}))
    assert again["row_version"] == line["row_version"]   # idempotent for the same approver
    assert (1, w["bob"].user_id, "APPROVED", "approver", ava_row, False) in decisions(run["id"])
    assert "RUN_SELF_LINE_APPROVED" in audit_actions(w["cid"], "payroll_run", run["id"])
    finalize(w["acme"], run["id"], key="self-pay-1")
    assert run_state(run["id"])["status"] == "FINALIZED"


# ---------------------------------------------------------------------------
# Action-time checks and concurrency (EC-RUN-07, EC-RUN-08)
# ---------------------------------------------------------------------------
def test_ec_run_07_a_grant_revoked_before_the_click_is_refused(fin_world):
    w = fin_world
    run = reviewed_run(w)
    submit(w["acme"], run["id"])
    r = w["acme"].put(f"/api/v1/users/{user_id_of(w['approver'])}/roles", json={"role_ids": []})
    assert r.status_code == 200, r.text
    r = approve(w["approver"], run["id"], expect=403)
    assert run_state(run["id"])["status"] == "APPROVAL_PENDING"
    approve(w["bob"], run["id"])
    assert run_state(run["id"])["status"] == "APPROVED"


def test_ec_run_08_two_concurrent_approvals_one_wins(app, fin_world):
    from tests.helpers import login

    w = fin_world
    run = reviewed_run(w)
    submit(w["acme"], run["id"])
    version = run_state(run["id"])["row_version"]
    clients = [login(app, w["approver"].email), login(app, w["bob"].email)]
    barrier = threading.Barrier(2)
    results = []

    def attempt(client):
        barrier.wait()
        r = client.post(f"{RUNS}/{run['id']}/approve", json={"row_version": version})
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=attempt, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [200, 409], results
    assert [c for s, c in results if s == 409][0] in ("ROW_VERSION_CONFLICT", "INVALID_TRANSITION")
    assert run_state(run["id"])["status"] == "APPROVED"
    assert len([d for d in decisions(run["id"]) if d[2] == "APPROVED"]) == 1
    r = approve(w["approver"], run["id"], expect=409, row_version=version)
    assert code_of(r) in ("ROW_VERSION_CONFLICT", "INVALID_TRANSITION")


def test_multi_step_chain_records_each_step_and_bars_the_earlier_approver(fin_world):
    w = fin_world
    approver_role = sql("SELECT id FROM roles WHERE company_id = :c AND system_key = 'payroll_approver'",
                        c=w["cid"])[0][0]
    sql("INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, role_id, created_at, "
        "updated_at) VALUES (:c, 'payroll_run', 1, 'role', :r, now(), now())", c=w["cid"], r=approver_role)
    sql("INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, specific_user_id, "
        "created_at, updated_at) VALUES (:c, 'payroll_run', 2, 'specific_user', :u, now(), now())",
        c=w["cid"], u=user_id_of(w["bob"]))
    run = reviewed_run(w)
    submit(w["acme"], run["id"])
    first = approve(w["approver"], run["id"], expected_step=1)
    assert first["status"] == "APPROVAL_PENDING" and first["approval_request"]["current_step"] == 2
    r = approve(w["approver"], run["id"], expect=403, expected_step=2)  # already approved step 1
    r = approve(w["bob"], run["id"], expect=409, expected_step=1)        # the request moved on
    assert code_of(r) == "APPROVAL_STEP_CHANGED"
    final = approve(w["bob"], run["id"], expected_step=2)
    assert final["status"] == "APPROVED"
    assert [(d[0], d[1], d[2]) for d in decisions(run["id"])] == [
        (1, user_id_of(w["approver"]), "APPROVED"), (2, w["bob"].user_id, "APPROVED")]
    assert audit_actions(w["cid"], "payroll_run", run["id"]).count("RUN_STEP_APPROVED") == 2


def test_a_decision_in_the_generic_inbox_moves_the_run_and_cancel_there_is_refused(fin_world):
    w = fin_world
    run = reviewed_run(w)
    submitted = submit(w["processor"], run["id"])
    r = w["processor"].post(f"{APPROVALS}/{submitted['approval_request_id']}/cancel", json={})
    assert r.status_code == 409 and code_of(r) == "USE_REOPEN"
    assert run_state(run["id"])["status"] == "APPROVAL_PENDING"
    ok(w["approver"].post(f"{APPROVALS}/{submitted['approval_request_id']}/approve", json={}))
    assert run_state(run["id"])["status"] == "APPROVED"
    assert decisions(run["id"]) == [(1, user_id_of(w["approver"]), "APPROVED", "approver", None, False)]


# ---------------------------------------------------------------------------
# Reject, rework, reopen (EC-RUN-16)
# ---------------------------------------------------------------------------
def test_reject_needs_a_reason_then_rework_clears_the_round_and_resubmission_starts_over(fin_world):
    w = fin_world
    run = reviewed_run(w)
    submit(w["acme"], run["id"])
    r = w["approver"].post(f"{RUNS}/{run['id']}/reject", json={"reason": "  ",
                                                              "row_version": run_state(run["id"])["row_version"]})
    assert r.status_code == 422
    assert reject(w["approver"], run["id"], reason="Overtime of Emma is missing")["status"] == "REJECTED"
    assert decisions(run["id"]) == [(1, user_id_of(w["approver"]), "REJECTED", "approver", None, False)]
    assert request_status(run["id"]) == ["REJECTED"]
    assert code_of(approve(w["bob"], run["id"], expect=409)) == "INVALID_TRANSITION"
    assert w["approver"].post(f"{RUNS}/{run['id']}/rework", json={}).status_code == 403  # payroll:run:calculate
    reworked = ok(w["processor"].post(f"{RUNS}/{run['id']}/rework", json={"note": "Overtime added"}))
    assert reworked["status"] == "REVIEW_REQUIRED"
    assert all(d[5] for d in decisions(run["id"]))  # the rejected round is cleared, kept as history
    submit(w["acme"], run["id"])
    approve(w["approver"], run["id"])
    assert request_status(run["id"]) == ["REJECTED", "APPROVED"]
    live = [d for d in decisions(run["id"]) if not d[5]]
    assert live == [(1, user_id_of(w["approver"]), "APPROVED", "approver", None, False)]


def test_ec_run_16_reopen_clears_approvals_and_returns_to_review_or_draft(fin_world):
    w = fin_world
    acme = w["acme"]
    run = reviewed_run(w)
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    assert w["approver"].post(f"{RUNS}/{run['id']}/reopen", json={"reason": "x"}).status_code == 403
    assert acme.post(f"{RUNS}/{run['id']}/reopen", json={}).status_code == 422  # reason required
    reopened = ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Bonus missing",
                                                                "target": "REVIEW_REQUIRED"}))
    assert reopened["status"] == "REVIEW_REQUIRED"
    assert all(d[5] for d in decisions(run["id"]))      # approvals cleared ...
    actions = audit_actions(w["cid"], "payroll_run", run["id"])
    assert "RUN_REOPENED" in actions                    # ... and audited
    cleared = sql("SELECT details FROM audit_logs WHERE entity_id = :r AND action = 'RUN_REOPENED' ORDER BY id",
                  r=str(run["id"]))[0][0]
    assert cleared["approvals_cleared"] == 1 and cleared["reason"] == "Bonus missing"
    # From APPROVAL_PENDING: the pending chain is withdrawn; then on to DRAFT (inputs released).
    submit(acme, run["id"])
    assert request_status(run["id"])[-1] == "PENDING"
    held = ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Wrong period"}))
    assert held["status"] == "REOPENED_FOR_CORRECTION" and request_status(run["id"])[-1] == "CANCELLED"
    r = acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Wrong period"})
    assert r.status_code == 400 and code_of(r) == "REOPEN_TARGET_REQUIRED"
    drafted = ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Re-lock inputs", "target": "DRAFT"}))
    assert drafted["status"] == "DRAFT"


# ---------------------------------------------------------------------------
# Every forbidden transition (3.5 §4.1)
# ---------------------------------------------------------------------------
FORBIDDEN = {
    # endpoint: states from which it is refused (409)
    "submit": ("DRAFT", "READY", "CALCULATING", "APPROVAL_PENDING", "APPROVED", "REJECTED",
               "REOPENED_FOR_CORRECTION"),
    "approve": ("DRAFT", "REVIEW_REQUIRED", "APPROVED", "REJECTED", "REOPENED_FOR_CORRECTION"),
    "reject": ("DRAFT", "REVIEW_REQUIRED", "APPROVED", "REJECTED"),
    "rework": ("DRAFT", "REVIEW_REQUIRED", "APPROVAL_PENDING", "APPROVED"),
    "finalize": ("DRAFT", "READY", "REVIEW_REQUIRED", "APPROVAL_PENDING", "REJECTED", "REOPENED_FOR_CORRECTION"),
    "close": ("DRAFT", "REVIEW_REQUIRED", "APPROVAL_PENDING", "APPROVED", "PAYMENT_READY", "SUBMITTED",
              "PARTIALLY_PAID", "PAID"),
}
FINALIZED_STATES = ("FINALIZED", "PAYMENT_READY", "SUBMITTED", "PARTIALLY_PAID", "PAID", "RECONCILED", "CLOSED")


def set_status(run_id, status):
    """Put a run in ``status`` directly (as the owner, guards off) to probe the refused moves."""
    from app.database.db import system_engine

    finalized = status in FINALIZED_STATES
    with system_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE payroll_runs SET status = :s, finalized_at = CASE WHEN :f THEN now() END, "
                          "cancelled_at = CASE WHEN :s = 'CANCELLED' THEN now() END WHERE id = :r"),
                     {"s": status, "f": finalized, "r": run_id})


def _post(w, endpoint, run_id):
    client = {"submit": w["acme"], "approve": w["approver"], "reject": w["approver"], "rework": w["processor"],
              "finalize": w["acme"], "close": w["acme"], "reopen": w["acme"]}[endpoint]
    version = run_state(run_id)["row_version"]
    body = {"submit": {}, "approve": {"row_version": version}, "reject": {"reason": "No", "row_version": version},
            "rework": {}, "finalize": {"confirm_label": run_state(run_id)["label"]}, "close": {},
            "reopen": {"reason": "Fix", "target": "DRAFT"}}[endpoint]
    return client.post(f"{RUNS}/{run_id}/{endpoint}", json=body, headers={"Idempotency-Key": f"k-{endpoint}-{version}"})


def test_every_forbidden_transition_is_a_409_and_changes_nothing(fin_world):
    w = fin_world
    run = reviewed_run(w)
    for endpoint, states in FORBIDDEN.items():
        for state in states:
            set_status(run["id"], state)
            before = run_state(run["id"])
            r = _post(w, endpoint, run["id"])
            expected = "RUN_FINALIZED" if state in FINALIZED_STATES else "INVALID_TRANSITION"
            assert r.status_code == 409, (endpoint, state, r.text)
            assert code_of(r) in (expected, "APPROVAL_NOT_PENDING"), (endpoint, state, r.text)
            assert run_state(run["id"]) == before, (endpoint, state)
    for state in FINALIZED_STATES:
        set_status(run["id"], state)
        r = _post(w, "reopen", run["id"])
        assert r.status_code == 409 and code_of(r) == "RUN_FINALIZED", (state, r.text)
        assert r.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION"
    set_status(run["id"], "CANCELLED")
    for endpoint in ("submit", "approve", "reject", "rework", "finalize", "close", "reopen"):
        r = _post(w, endpoint, run["id"])
        assert r.status_code == 409 and code_of(r) == "RUN_CANCELLED", (endpoint, r.text)


# ---------------------------------------------------------------------------
# Isolation (EC-SEC-01, EC-SEC-02)
# ---------------------------------------------------------------------------
def test_ec_sec_01_approval_endpoints_and_rows_are_isolated_across_three_tenants(fin_world):
    w = fin_world
    run = reviewed_run(w)
    submit(w["acme"], run["id"])
    approve(w["approver"], run["id"])
    row = run_employees(run["id"])[w["emp"]["Emma"]["id"]][2]
    for other in ("beta", "cobalt"):
        client = w[other]["client"]
        for endpoint in ("submit", "approve", "reject", "rework", "reopen", "finalize", "close"):
            body = {"approve": {"row_version": 1}, "reject": {"reason": "x", "row_version": 1},
                    "reopen": {"reason": "x"}, "finalize": {"confirm_label": "x"}}.get(endpoint, {})
            r = client.post(f"{RUNS}/{run['id']}/{endpoint}", json=body, headers={"Idempotency-Key": f"o-{endpoint}"})
            assert r.status_code in (403, 404), (other, endpoint, r.text)
            assert r.status_code == 404 or code_of(r) in ("FORBIDDEN", "STEP_UP_REQUIRED"), (other, endpoint)
        assert client.post(f"{RUNS}/{run['id']}/employees/{row}/approve-self-line", json={}).status_code in (403, 404)
        assert client.get(f"{RUNS}/{run['id']}/approvals").status_code in (403, 404)
        with tenant(w[other]["cid"]) as conn:
            assert conn.execute(text("SELECT count(*) FROM payroll_approvals")).scalar() == 0
            assert conn.execute(text("UPDATE payroll_approvals SET comment = 'x'")).rowcount == 0
    # Their own run is invisible to Acme as well.
    beta_run = new_run(w["beta"]["client"], w["beta"]["group"], w["beta"]["period"]["id"])
    assert w["acme"].post(f"{RUNS}/{beta_run['id']}/submit", json={}).status_code == 404
    assert w["acme"].get(f"{RUNS}/{beta_run['id']}/approvals").status_code == 404
    assert run_state(run["id"])["status"] == "APPROVED"
