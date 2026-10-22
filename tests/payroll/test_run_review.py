"""Reviewing a calculated run: summary, employees, "Why this amount?", variance and its acknowledgment,
stale inputs (3.5 §5.5-§5.6, §8.3, §10; 3.8 §5.3-§5.4; 3.9 §5.4).

EC-RUN-11 (population reconciliation), EC-SEC-05 (counts and totals honour the employee scope), EC-UX-08
(source links respect the viewer's permissions), EC-RUN-09 (stale inputs), EC-STA-01 (capability labels),
EC-RUN-17 (no acknowledgment on a finalized run), EC-SEC-02 (foreign ids are 404).
"""

from datetime import datetime, timezone
from decimal import Decimal

from tests.payroll.test_components import ok
from tests.payroll.test_run_calculation import (  # noqa: F401  (fixtures)
    RUNS,
    calc_world,
    calculate,
    calculated,
    findings,
    key,
    run_state,
    runs_world,
    standin,
)
from tests.helpers import create_employee
from tests.payroll.test_runs_lifecycle import force_status, new_run
from tests.payroll.test_wage_inputs import audit_actions, role, sql, user_with_role

D = Decimal
FIXTURE_LABEL = "FIXTURE — not law"
NOT_VERIFIED = "Compliance not verified"


def reader(app, w, name, scope, targets, codes=("payroll:run:read",)):
    """A user with ``codes`` at ``scope`` (LEGAL_ENTITY or PAY_GROUP targets)."""
    role_id = role(w["acme"], f"{name} role", scope, codes)
    client = user_with_role(app, w["acme"], create_employee(w["acme"], name, hire_date="2024-01-15"), role_id)
    field = "legal_entity_ids" if scope == "LEGAL_ENTITY" else "pay_group_ids"
    r = w["acme"].put(f"/api/v1/users/{client.user_id}/scope-targets",
                      json={"assignments": [{"role_id": role_id, field: targets}]})
    assert r.status_code == 200, r.text
    return client


def add_foreign_row(w):
    """Gus (placed in AE01 / PG-AE) added to the PG-IN run as the database owner, to give the run an employee
    outside the PG-IN and LE01 scopes (EC-SEC-05)."""
    gus = w["gus"]["id"]
    assignment = sql("SELECT id FROM employee_assignments WHERE employee_id = :e ORDER BY id DESC LIMIT 1", e=gus)[0][0]
    return sql("INSERT INTO payroll_run_employees (company_id, run_id, employee_id, employee_assignment_id, status, "
               "currency, flags, stage_outputs, row_version, created_at, updated_at) VALUES (:c, :r, :e, :a, "
               "'PENDING', 'INR', '[]', '{}', 1, now(), now()) RETURNING id", c=w["cid"], r=w["run"], e=gus,
               a=assignment)[0][0]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def test_summary_totals_population_settings_and_labels_ec_run_11(calc_world):
    w = calc_world
    w["standin"].config = {w["emma"]: {"bonus": "3000.00"}}
    calculated(w)
    summary = ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))
    # By hand: Emma 97,000 + 3,000 bonus = 1,00,000.00; Ivan 97,000.00 -> 1,97,000.00 per currency.
    assert summary["totals"] == {"INR": {"gross": "197000.00", "deductions": "0.00", "reimbursements": "0.00",
                                         "net": "197000.00", "employer_cost": "197000.00", "employees": 2}}
    population = summary["population"]
    assert summary["reconciles"] and population["eligible"] == population["included"] + population["excluded"]
    assert population["excluded_by_reason"] == {"NO_SALARY": 2} and population["calculated"] == 2
    assert summary["settings"]["payroll.lop.proration_basis"] == "CALENDAR_DAYS"
    assert summary["settings"]["payroll.approval.variance_review_threshold_percent"] == "20"
    assert summary["pack"]["country"] == "GB" and NOT_VERIFIED in summary["pack"]["labels"]
    assert summary["liabilities"] == []
    assert summary["calculation"]["engine_version"] and summary["calculation"]["stale"] is False
    assert summary["run"]["status"] == "REVIEW_REQUIRED"


def test_ec_sta_01_summary_labels_compliance_not_verified_and_fixture(calc_world):
    w = calc_world
    ae_run = new_run(w["acme"], w["ae_group"], _period_of(w, w["ae_group"]))
    pack = ok(w["acme"].get(f"{RUNS}/{ae_run['id']}/summary"))["pack"]
    assert pack["country"] == "AE" and pack["verified"] is False and NOT_VERIFIED in pack["labels"]
    assert pack["capability"] == "CONFIG_ONLY"
    sql("UPDATE payroll_runs SET pack_version_ids = '[{\"code\": \"IN\", \"version\": \"fixture\", "
        "\"is_fixture\": true}]'::jsonb WHERE id = :r", r=w["run"])
    pack = ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))["pack"]
    assert FIXTURE_LABEL in pack["labels"] and pack["versions"][0]["label"] == FIXTURE_LABEL


def _period_of(w, group):
    from tests.payroll.test_runs_lifecycle import current_period

    return current_period(w["acme"], group)["id"]


def test_ec_sec_05_summary_and_lists_count_only_employees_in_scope(app, calc_world):
    w = calc_world
    calculated(w)
    foreign = add_foreign_row(w)
    sql("UPDATE payroll_run_employees SET status = 'CALCULATED', gross = 500, deductions = 0, reimbursements = 0, "
        "net = 500, employer_cost = 500, calc_hash = repeat('c', 64) WHERE id = :e", e=foreign)
    company = ok(w["acme"].get(f"{RUNS}/{w['run']}/summary"))
    assert company["population"]["eligible"] == 5 and company["totals"]["INR"]["gross"] == "194500.00"
    clerk = reader(app, w, "Lena", "LEGAL_ENTITY", [w["le01"]])
    scoped = ok(clerk.get(f"{RUNS}/{w['run']}/summary"))
    assert scoped["population"]["eligible"] == 4                      # Gus is outside LE01
    assert scoped["totals"]["INR"] == {"gross": "194000.00", "deductions": "0.00", "reimbursements": "0.00",
                                       "net": "194000.00", "employer_cost": "194000.00", "employees": 2}
    listed = ok(clerk.get(f"{RUNS}/{w['run']}/employees"))
    assert listed["total"] == 4 and foreign not in {i["id"] for i in listed["items"]}
    assert listed["counts"]["calculated"] == 2
    assert clerk.get(f"{RUNS}/{w['run']}/employees/{foreign}").status_code == 404
    assert ok(w["acme"].get(f"{RUNS}/{w['run']}/employees"))["total"] == 5
    # A reader of another entity does not see the run at all.
    other = reader(app, w, "Omar", "LEGAL_ENTITY", [w["ae"]])
    assert other.get(f"{RUNS}/{w['run']}/summary").status_code == 404


def test_review_reads_need_run_read_permission(app, calc_world):
    w = calc_world
    calculated(w)
    nobody = reader(app, w, "Nadia", "LEGAL_ENTITY", [w["le01"]], codes=("payroll:inputs:read",))
    for path in ("summary", "employees", f"employees/{w['emma_row']}", "variance", "stale"):
        assert nobody.get(f"{RUNS}/{w['run']}/{path}").status_code == 403, path
    assert ok(w["approver"].get(f"{RUNS}/{w['run']}/summary"))["totals"]["INR"]["employees"] == 2


# ---------------------------------------------------------------------------
# Employees and "Why this amount?"
# ---------------------------------------------------------------------------
def test_employee_list_search_filters_and_totals(calc_world):
    w = calc_world
    w["standin"].config = {w["ivan"]: {"broken": True}}
    calculated(w, "VALIDATION_FAILED")
    listed = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees", params={"search": "emm"}))
    assert [i["employee_id"] for i in listed["items"]] == [w["emma"]]
    emma = listed["items"][0]
    assert emma["net"] == {"amount": "97000.00", "currency": "INR"} and emma["status"] == "CALCULATED"
    errors = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees", params={"filter": "errors"}))
    assert [i["employee_id"] for i in errors["items"]] == [w["ivan"]] and errors["items"][0]["net"] is None
    assert errors["counts"]["errors"] == 1 and errors["counts"]["excluded"] == 2
    held = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees", params={"filter": "holds"}))
    assert held["total"] == 0
    assert w["acme"].get(f"{RUNS}/{w['run']}/employees", params={"filter": "bogus"}).status_code == 422


def test_employee_detail_lines_explanations_and_ledger(calc_world):
    w = calc_world
    w["standin"].config = {w["emma"]: {"lop": 2, "bonus": "1000.00"}}
    calculated(w)
    detail = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    codes = [line["component"] for line in detail["lines"]]
    assert codes[:3] == ["BASIC", "HRA", "FIXED_ALLOWANCE"] and "BONUS" in codes
    basic = detail["lines"][0]
    assert "2 LOP days" in basic["explanation"] and basic["trace"]["method"]
    assert basic["amount"]["currency"] == "INR" and Decimal(basic["amount"]["amount"]) < D("50000")
    assert detail["ledger"] and detail["ledger"]["days"]
    assert detail["snapshot"]["version"] == 1 and detail["calc_hash"] and detail["engine_version"]
    # The payroll manager follows every source: compensation, day ledger, wage input.
    sources = {s["type"]: s for line in detail["lines"] for s in line["sources"]}
    assert sources["compensation_assignment"]["link"].startswith("/payroll/compensation")
    assert sources["wage_input"]["id"] == 7001 and "link" in sources["wage_input"]
    assert detail["exceptions"] and all(e["run_employee_id"] == w["emma_row"] for e in detail["exceptions"])


def test_ec_ux_08_source_links_and_hr_only_lines_follow_the_viewer(app, calc_world):
    w = calc_world
    w["standin"].config = {w["emma"]: {"lop": 1, "bonus": "1000.00", "hr_only": True}}
    calculated(w)
    group_reader = reader(app, w, "Gita", "PAY_GROUP", [w["group"]])
    detail = ok(group_reader.get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    codes = {line["component"] for line in detail["lines"]}
    assert "HRA" not in codes and detail["hidden_lines"] == 1       # HR_ONLY: company / entity scope only
    sources = [s for line in detail["lines"] for s in line["sources"]]
    assert sources and all(s.get("restricted") and "id" not in s and "link" not in s for s in sources)
    ledger_refs = _refs(detail["ledger"])
    assert all("id" not in ref for ref in ledger_refs if ref.get("type") in ("manual_lop", "leave", "attendance"))
    entity_reader = reader(app, w, "Ezra", "LEGAL_ENTITY", [w["le01"]])
    full = ok(entity_reader.get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    assert "HRA" in {line["component"] for line in full["lines"]} and full["hidden_lines"] == 0
    manager = ok(w["acme"].get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}"))
    assert any(s.get("id") == 7001 for line in manager["lines"] for s in line["sources"])


def _refs(value):
    found = []
    if isinstance(value, dict):
        if "type" in value and ("id" in value or value.get("restricted")):
            found.append(value)
        for item in value.values():
            found.extend(_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_refs(item))
    return found


def test_employee_detail_of_another_run_or_tenant_is_404(calc_world):
    w = calc_world
    calculated(w)
    other = new_run(w["acme"], w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[w["emma"]])
    assert w["acme"].get(f"{RUNS}/{other['id']}/employees/{w['emma_row']}").status_code == 404
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    beta_row = sql("SELECT id FROM payroll_run_employees WHERE run_id = :r", r=beta_run["id"])[0][0]
    assert w["acme"].get(f"{RUNS}/{w['run']}/employees/{beta_row}").status_code == 404
    assert beta["client"].get(f"{RUNS}/{w['run']}/employees/{w['emma_row']}").status_code == 404


# ---------------------------------------------------------------------------
# Variance (3.5 §5.5-§5.6, §10)
# ---------------------------------------------------------------------------
def test_variance_flags_need_acknowledgment_with_a_comment_for_net_changes(calc_world):
    w = calc_world
    # Emma: previous net 60,000 -> 97,000 (+61.67% > 20%); BASIC 30,000 -> 50,000 (+66.67%).
    # Ivan: previous identical to now (97,000; components equal) -> no flag.
    w["standin"].config = {
        w["emma"]: {"previous": {"gross": "60000.00", "net": "60000.00",
                                 "components": {"BASIC": "30000.00", "HRA": "20000.00", "FIXED_ALLOWANCE": "10000.00"}}},
        w["ivan"]: {"previous": {"gross": "97000.00", "net": "97000.00",
                                 "components": {"BASIC": "50000.00", "HRA": "20000.00", "FIXED_ALLOWANCE": "27000.00"}}},
    }
    calculated(w)
    view = ok(w["acme"].get(f"{RUNS}/{w['run']}/variance"))
    assert view["total"] == 2 and view["flagged"] == 1 and view["pending"] == 1
    emma = next(i for i in view["items"] if i["employee_id"] == w["emma"])
    # By hand: |97,000 − 60,000| / 60,000 × 100 = 61.666… -> 61.67
    assert emma["net"] == {"previous": "60000.00", "current": "97000.00", "change": "37000.00",
                           "change_percent": "61.67", "flagged": True}
    basic = next(c for c in emma["components"] if c["component"] == "BASIC")
    assert basic["change_percent"] == "66.67" and basic["flagged"]
    fixed = next(c for c in emma["components"] if c["component"] == "FIXED_ALLOWANCE")
    assert fixed["change_percent"] == "170.00" and fixed["flagged"]          # 10,000 -> 27,000
    assert emma["comment_required"] and set(emma["pending"]) == {"NET_VARIANCE", "COMPONENT_VARIANCE"}
    ivan = next(i for i in view["items"] if i["employee_id"] == w["ivan"])
    assert not ivan["flagged"] and ivan["net"]["change_percent"] == "0.00"
    flagged_only = ok(w["acme"].get(f"{RUNS}/{w['run']}/variance", params={"only_flagged": True}))
    assert [i["employee_id"] for i in flagged_only["items"]] == [w["emma"]]

    from app.payroll.services import review
    from tests.payroll.test_runs_lifecycle import session_for

    def acknowledged():
        db = session_for(w["cid"])
        try:
            from app.models.pay_runs import PayrollRun

            return review.variance_flags_acknowledged(db, db.get(PayrollRun, w["run"]))
        finally:
            db.close()

    assert acknowledged() is False
    url = f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge"
    r = w["acme"].post(url, json={})
    assert r.status_code == 400 and r.json()["code"] == "COMMENT_REQUIRED"
    r = w["acme"].post(url, json={"comment": "Promotion effective this month."}, headers={"Idempotency-Key": key()})
    assert r.status_code == 200, r.text
    assert r.json()["remaining"] == 0 and set(r.json()["codes"]) == {"NET_VARIANCE", "COMPONENT_VARIANCE"}
    assert acknowledged() is True
    again = w["acme"].post(url, json={"comment": "again"})
    assert again.status_code == 409 and again.json()["code"] == "NOTHING_TO_ACKNOWLEDGE"
    view = ok(w["acme"].get(f"{RUNS}/{w['run']}/variance"))
    emma = next(i for i in view["items"] if i["employee_id"] == w["emma"])
    assert emma["acknowledged"] and emma["acknowledgments"][0]["comment"] == "Promotion effective this month."
    assert "VARIANCE_ACKNOWLEDGED" in audit_actions(w["cid"], "payroll_run_employee", w["emma_row"])
    # Recalculating Emma raises the flags again: the acknowledgment belonged to the old figures.
    calculate(w["acme"], w["run"], employee_ids=[w["emma"]])
    assert acknowledged() is False


def test_component_only_variance_is_acknowledged_without_a_comment(calc_world):
    w = calc_world
    # Net unchanged (97,000) but BASIC 40,000 -> 50,000 (+25%) and FIXED_ALLOWANCE 37,000 -> 27,000 (−27.03%).
    w["standin"].config = {w["emma"]: {"previous": {
        "gross": "97000.00", "net": "97000.00",
        "components": {"BASIC": "40000.00", "HRA": "20000.00", "FIXED_ALLOWANCE": "37000.00"}}}}
    calculated(w)
    view = ok(w["acme"].get(f"{RUNS}/{w['run']}/variance", params={"only_flagged": True}))
    emma = view["items"][0]
    assert not emma["net"]["flagged"] and not emma["comment_required"]
    assert {c["component"]: c["change_percent"] for c in emma["components"] if c["flagged"]} == {
        "BASIC": "25.00", "FIXED_ALLOWANCE": "27.03"}
    r = w["acme"].post(f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge", json={})
    assert r.status_code == 200 and r.json()["codes"] == ["COMPONENT_VARIANCE"]


def test_acknowledgment_rules_self_pay_scope_state_and_permission(app, calc_world):
    w = calc_world
    w["standin"].config = {w["emma"]: {"previous": {"gross": "10.00", "net": "10.00", "components": {}}},
                           w["ivan"]: {"previous": {"gross": "10.00", "net": "10.00", "components": {}}}}
    calculated(w)
    from tests.payroll.test_compensation import login_as

    emma_client = login_as(app, w["acme"], w["emp"]["Emma"], ["payroll_processor"])
    r = emma_client.post(f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge", json={"comment": "mine"})
    assert r.status_code == 403                                     # nobody acknowledges their own pay
    r = w["approver"].post(f"{RUNS}/{w['run']}/variance/{w['ivan_row']}/acknowledge", json={"comment": "x"})
    assert r.status_code == 403                                     # payroll:run:review needed
    r = w["acme"].post(f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge",
                       json={"comment": "ok", "row_version": 1})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"
    r = w["acme"].post(f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge", json={"comment": "x", "by": 1})
    assert r.status_code == 422                                     # strict body (EC-SEC-06)
    beta = w["beta"]
    assert beta["client"].post(f"{RUNS}/{w['run']}/variance/{w['emma_row']}/acknowledge",
                               json={"comment": "x"}).status_code == 404
    force_status(w["run"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    r = w["acme"].post(f"{RUNS}/{w['run']}/variance/{w['ivan_row']}/acknowledge", json={"comment": "late"})
    assert r.status_code == 409 and r.json()["code"] == "RUN_FINALIZED"  # EC-RUN-17


# ---------------------------------------------------------------------------
# Stale inputs (EC-RUN-09)
# ---------------------------------------------------------------------------
def test_ec_run_09_stale_view_lists_affected_employees_and_recalculation_clears_it(calc_world):
    w = calc_world
    calculated(w)
    from app.payroll.services import runs as run_service
    from tests.payroll.test_runs_lifecycle import session_for

    db = session_for(w["cid"])
    try:
        assert run_service.mark_stale(db, w["cid"], "Attendance corrected after lock.",
                                      employee_ids=[w["ivan"]], details={"employee_id": w["ivan"]}) == 1
        db.commit()
    finally:
        db.close()
    view = ok(w["acme"].get(f"{RUNS}/{w['run']}/stale"))
    assert view["stale"] is True and view["reasons"][0]["reason"] == "Attendance corrected after lock."
    assert [e["employee_id"] for e in view["employees"]] == [w["ivan"]]
    recalc = next(a for a in view["actions"] if a["action"] == "RECALCULATE_SELECTED")
    assert recalc["available"] and recalc["employee_ids"] == [w["ivan"]]
    assert next(a for a in view["actions"] if a["action"] == "HANDLE_AS_RETRO")["available"] is False
    calculate(w["acme"], w["run"], employee_ids=[w["emma"]])          # does not cover Ivan's change
    assert run_state(w["run"])[5] is True
    calculate(w["acme"], w["run"], employee_ids=recalc["employee_ids"])
    assert run_state(w["run"])[5] is False
    assert ok(w["acme"].get(f"{RUNS}/{w['run']}/stale"))["employees"] == []
