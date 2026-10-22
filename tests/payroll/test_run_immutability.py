"""A finalized run is never edited (3.5 §4.1, EC-RUN-17): every service write answers 409 RUN_FINALIZED with the
"create a correction" hint, and the database refuses changes to its results, lines, snapshots, consumed sources
and approval history even for a caller that bypasses the services.

EC-RUN-17, EC-RUN-13, EC-VAR-01, EC-SEC-01.
"""

from sqlalchemy import text

from tests.payroll.test_components import ok
from tests.payroll.test_run_finalize import (  # noqa: F401  (fixtures)
    approve,
    approved_run,
    code_of,
    fin_world,
    finalize,
    prepare,
    run_state,
    submit,
)
from tests.payroll.test_runs_lifecycle import PAY, RUNS, new_run, run_employees, runs_world  # noqa: F401
from tests.payroll.test_wage_inputs import CHECK, expect_error, sql, tenant


def finalized_run(w, **calc):
    run = approved_run(w, **calc)
    finalize(w["acme"], run["id"], key=f"imm-{run['id']}")
    assert run_state(run["id"])["status"] == "FINALIZED"
    return run


def test_ec_run_17_every_service_write_to_a_finalized_run_is_409_with_the_correction_hint(fin_world):
    w = fin_world
    acme = w["acme"]
    run = finalized_run(w)
    rid = run["id"]
    version = run_state(rid)["row_version"]
    emma_row = run_employees(rid)[w["emp"]["Emma"]["id"]][2]
    attempts = [
        (acme, f"{RUNS}/{rid}/submit", {}),
        (w["approver"], f"{RUNS}/{rid}/approve", {"row_version": version}),
        (w["approver"], f"{RUNS}/{rid}/reject", {"reason": "late", "row_version": version}),
        (w["processor"], f"{RUNS}/{rid}/rework", {}),
        (acme, f"{RUNS}/{rid}/reopen", {"reason": "Fix BASIC", "target": "REVIEW_REQUIRED"}),
        (acme, f"{RUNS}/{rid}/cancel", {"reason": "Wrong period"}),
        (acme, f"{RUNS}/{rid}/precheck", {}),
        (acme, f"{RUNS}/{rid}/finalize", {"confirm_label": run_state(rid)["label"]}),
        (w["bob"], f"{RUNS}/{rid}/employees/{emma_row}/approve-self-line", {}),
    ]
    for client, url, body in attempts:
        r = client.post(url, json=body, headers={"Idempotency-Key": f"imm-{url.rsplit('/', 1)[1]}"})
        assert r.status_code == 409, (url, r.text)
        assert code_of(r) == "RUN_FINALIZED" and r.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION", (url, r.text)
    assert run_state(rid)["row_version"] == version and run_state(rid)["status"] == "FINALIZED"


def test_ec_run_17_database_refuses_changes_to_a_finalized_runs_results_lines_snapshots_and_sources(fin_world):
    w = fin_world
    cid = w["cid"]
    run = finalized_run(w)
    rid = run["id"]
    with tenant(cid) as conn:
        row = conn.execute(text("SELECT id FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED' "
                                "ORDER BY id LIMIT 1"), {"r": rid}).scalar()
        line = conn.execute(text("SELECT id FROM payroll_lines WHERE run_employee_id = :e ORDER BY id LIMIT 1"),
                            {"e": row}).scalar()
        snapshot = conn.execute(text("SELECT id FROM payroll_input_snapshots WHERE run_employee_id = :e"),
                                {"e": row}).scalar()
        expect_error(conn, CHECK, "UPDATE payroll_lines SET amount = amount + 1 WHERE id = :l", l=line)
        expect_error(conn, CHECK, "DELETE FROM payroll_lines WHERE id = :l", l=line)
        expect_error(conn, CHECK, "INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, "
                                  "stage, amount, currency, tags, visibility, sources, trace, sort_order, statutory, "
                                  "fixture, created_at, updated_at) VALUES (:c, :r, :e, 'BONUS', 'EARNING', 4, 1, 'INR', "
                                  "'[]', 'EMPLOYEE_VISIBLE', '[]', '{}', 1, false, false, now(), now())",
                     c=cid, r=rid, e=row)
        expect_error(conn, CHECK, "UPDATE payroll_input_snapshots SET content_hash = content_hash WHERE id = :s",
                     s=snapshot)
        expect_error(conn, CHECK, "DELETE FROM payroll_input_snapshots WHERE id = :s", s=snapshot)
        for change in ("net = net + 1", "gross = 0", "status = 'ERROR'", "calc_hash = NULL",
                       "stage_outputs = '{}'::jsonb", "self_line_approved_by_user_id = NULL, snapshot_id = NULL"):
            expect_error(conn, CHECK, f"UPDATE payroll_run_employees SET {change} WHERE id = :e", e=row)
        expect_error(conn, CHECK, "DELETE FROM payroll_run_employees WHERE id = :e", e=row)
        expect_error(conn, CHECK, "UPDATE payroll_runs SET currency = 'USD' WHERE id = :r", r=rid)
        expect_error(conn, CHECK, "UPDATE payroll_runs SET totals = '{\"INR\": {}}'::jsonb WHERE id = :r", r=rid)
        expect_error(conn, CHECK, "UPDATE payroll_runs SET status = 'APPROVED', finalized_at = NULL WHERE id = :r",
                     r=rid)
        expect_error(conn, CHECK, "UPDATE payroll_approvals SET cleared_at = now() WHERE run_id = :r", r=rid)
        expect_error(conn, CHECK, "DELETE FROM payroll_approvals WHERE run_id = :r", r=rid)
        # Linking an unconsumed source to a finalized run is refused; a late consumed one (retro) is accepted.
        expect_error(conn, CHECK, "INSERT INTO payroll_line_sources (company_id, line_id, run_id, run_employee_id, "
                                  "source_type, source_id, source_key, consumed, created_at, updated_at) VALUES "
                                  "(:c, :l, :r, :e, 'ARREARS', 991, '', false, now(), now())", c=cid, l=line, r=rid, e=row)
        source = conn.execute(text(
            "INSERT INTO payroll_line_sources (company_id, line_id, run_id, run_employee_id, source_type, source_id, "
            "source_key, consumed, consumed_at, created_at, updated_at) VALUES (:c, :l, :r, :e, 'ARREARS', 992, '', "
            "true, now(), now(), now()) RETURNING id"), {"c": cid, "l": line, "r": rid, "e": row}).scalar()
        expect_error(conn, CHECK, "UPDATE payroll_line_sources SET consumed = false, consumed_at = NULL WHERE id = :s",
                     s=source)
        expect_error(conn, CHECK, "DELETE FROM payroll_line_sources WHERE id = :s", s=source)
        # A payment hold may still be placed on a finalized employee (EC-RUN-13): only the hold columns change.
        held = conn.execute(text(
            "UPDATE payroll_run_employees SET hold_type = 'PAYMENT', hold = '{\"type\": \"PAYMENT\"}'::jsonb, "
            "row_version = row_version + 1 WHERE id = :e"), {"e": row}).rowcount
        assert held == 1


def test_the_same_changes_are_allowed_before_finalization(fin_world):
    """Negative control: the guards only bite once the run is finalized."""
    w = fin_world
    run = approved_run(w)
    rid = run["id"]
    with tenant(w["cid"]) as conn:
        row = conn.execute(text("SELECT id FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED' "
                                "ORDER BY id LIMIT 1"), {"r": rid}).scalar()
        assert conn.execute(text("UPDATE payroll_lines SET explanation = 'Reviewed' WHERE run_employee_id = :e"),
                            {"e": row}).rowcount >= 1
        assert conn.execute(text("UPDATE payroll_run_employees SET flags = '[\"CHECKED\"]'::jsonb WHERE id = :e"),
                            {"e": row}).rowcount == 1
        snapshot = conn.execute(text("SELECT id FROM payroll_input_snapshots WHERE run_employee_id = :e"),
                                {"e": row}).scalar()
        # Snapshots are never updated (a re-snapshot is a new version), but a draft run may drop them.
        expect_error(conn, CHECK, "UPDATE payroll_input_snapshots SET content_hash = content_hash WHERE id = :s",
                     s=snapshot)
    assert run_state(rid)["status"] == "APPROVED"


def test_ec_var_01_a_reopened_run_keeps_nothing_consumed(fin_world):
    """Consumption happens only at finalization: reopening before it consumes nothing; finalizing afterwards
    consumes once."""
    w = fin_world
    acme = w["acme"]
    emma = w["emp"]["Emma"]["id"]
    from tests.payroll.test_run_finalize import _wage_input, line

    bonus = _wage_input(w, emma)
    run = new_run(acme, w["group"], w["period"]["id"])
    prepare(acme, run["id"], {emma: [line("BASIC", "50000.00"), line("BONUS", "5000.00", category="BONUS",
                                                                     sources=[{"type": "wage_input", "id": bonus}])]})
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    ok(acme.post(f"{RUNS}/{run['id']}/reopen", json={"reason": "Add the bonus note", "target": "REVIEW_REQUIRED"}))
    assert sql("SELECT status FROM wage_inputs WHERE id = :i", i=bonus)[0][0] == "APPROVED"
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=run["id"])[0][0] == 0
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    finalize(acme, run["id"], key="reopened-1")
    assert sql("SELECT status FROM wage_inputs WHERE id = :i", i=bonus)[0][0] == "CONSUMED"
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=run["id"])[0][0] == 1
