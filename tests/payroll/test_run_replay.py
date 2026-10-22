"""Replay of a calculated run from its stored snapshots (3.5 §8.2, §8.4; EC-STA-04).

Replay recalculates every employee from the frozen snapshot with the stored engine version, compares
``calc_hash`` and never writes. It needs payroll:run:read in the run's scope plus payroll:audit:read or
payroll:run:finalize; another tenant's run is 404 (EC-SEC-02).
"""

from datetime import datetime, timezone

from tests.helpers import create_employee
from tests.payroll.test_components import ok
from tests.payroll.test_run_calculation import (  # noqa: F401  (fixtures)
    RUNS,
    calc_world,
    calculated,
    runs_world,
    standin,
)
from tests.payroll.test_runs_lifecycle import force_status, new_run
from tests.payroll.test_wage_inputs import role, sql, user_with_role

WATCHED = ("payroll_runs", "payroll_run_employees", "payroll_lines", "payroll_line_sources", "payroll_exceptions",
           "payroll_input_snapshots", "audit_logs", "jobs", "idempotency_keys")


def footprint(company_id):
    """Row counts and the latest change of every table a replay could touch."""
    result = {table: sql(f"SELECT count(*), max(id) FROM {table} WHERE company_id = :c", c=company_id)[0]
              for table in WATCHED}
    result["changed"] = sql("SELECT max(updated_at) FROM payroll_run_employees WHERE company_id = :c",
                            c=company_id)[0]
    return result


def replay(client, run_id, expect=200, **params):
    body = params.pop("body", None)
    r = client.post(f"{RUNS}/{run_id}/replay", params=params, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def test_ec_sta_04_replay_reproduces_calc_hash_and_never_writes(calc_world):
    w = calc_world
    w["standin"].config = {w["emma"]: {"bonus": "2500.00", "lop": 1}}
    calculated(w)
    before = footprint(w["cid"])
    result = replay(w["acme"], w["run"])
    assert footprint(w["cid"]) == before                       # nothing written, not even an audit row
    assert result["checked"] == result["matched"] == result["total"] == 2 and result["mismatched"] == 0
    stored = dict(sql("SELECT id, calc_hash FROM payroll_run_employees WHERE run_id = :r AND calc_hash IS NOT NULL",
                      r=w["run"]))
    for item in result["items"]:
        assert item["matches"] and item["content_hash_ok"]
        assert item["replayed_calc_hash"] == item["stored_calc_hash"] == stored[item["run_employee_id"]]
    assert result["engine_version"]["stored"] == result["engine_version"]["current"]


def test_ec_sta_04_replay_of_a_finalized_run_uses_its_frozen_snapshots(calc_world):
    w = calc_world
    calculated(w)
    force_status(w["run"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    w["standin"].config = {w["emma"]: {"ctc": "2400000"}}   # later changes never reach the frozen snapshots
    result = replay(w["acme"], w["run"])
    assert result["matched"] == 2


def test_replay_reports_a_mismatch_with_both_figures(calc_world):
    w = calc_world
    calculated(w)
    sql("UPDATE payroll_run_employees SET calc_hash = repeat('d', 64), net = 1 WHERE id = :e", e=w["ivan_row"])
    result = replay(w["acme"], w["run"])
    assert result["mismatched"] == 1
    bad = next(i for i in result["items"] if i["run_employee_id"] == w["ivan_row"])
    assert bad["matches"] is False and bad["stored_calc_hash"] == "d" * 64
    assert bad["stored_net"] == {"amount": "1.00", "currency": "INR"}
    assert bad["replayed_net"] == {"amount": "97000.00", "currency": "INR"}   # 12,00,000 Example A by hand


def test_replay_refuses_an_engine_version_it_does_not_have(calc_world):
    w = calc_world
    calculated(w)
    sql("UPDATE payroll_run_employees SET stage_outputs = jsonb_set(stage_outputs, '{engine_version}', '\"0.9.0\"') "
        "WHERE id = :e", e=w["emma_row"])
    result = replay(w["acme"], w["run"])
    emma = next(i for i in result["items"] if i["run_employee_id"] == w["emma_row"])
    assert emma["matches"] is False and emma["problem"] == "ENGINE_VERSION_UNAVAILABLE"


def test_replay_selection_and_paging(calc_world):
    w = calc_world
    calculated(w)
    only = replay(w["acme"], w["run"], body={"employee_ids": [w["ivan"]]})
    assert [i["employee_id"] for i in only["items"]] == [w["ivan"]]
    page = replay(w["acme"], w["run"], limit=1, skip=1)
    assert page["total"] == 2 and page["checked"] == 1
    replay(w["acme"], w["run"], expect=404, body={"employee_ids": [w["emp"]["Nora"]["id"]]})  # no result
    replay(w["acme"], w["run"], expect=404, body={"employee_ids": [w["beta"]["employee"]["id"]]})
    replay(w["acme"], w["run"], expect=422, body={"employee_ids": [w["emma"]], "write": True})


def test_replay_permissions_scope_and_tenants(app, calc_world):
    w = calc_world
    calculated(w)
    replay(w["approver"], w["run"], expect=403)            # payroll:run:read without audit/finalize
    replay(w["processor"], w["run"], expect=403)
    auditor_role = role(w["acme"], "Run auditor", "COMPANY", ("payroll:run:read", "payroll:audit:read"))
    auditor = user_with_role(app, w["acme"], create_employee(w["acme"], "Audra", hire_date="2024-01-15"),
                             auditor_role)
    assert replay(auditor, w["run"])["matched"] == 2
    replay(w["cleo"], w["run"], expect=404)                 # finalize on AE01 only: the LE01 run is out of scope
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    replay(w["acme"], beta_run["id"], expect=404)
    replay(beta["client"], w["run"], expect=404)
