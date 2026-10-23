"""Finalize and close pay runs (3.5 §4.1, §5.8, §5.10, §8.4, §16; decision I-15; FOLLOW-UPS F-15, F-34).

EC-RUN-04, EC-RUN-06, EC-RUN-09, EC-RUN-13, EC-STA-01, EC-STA-02, EC-STA-03, EC-STA-10, EC-STA-22, EC-ORG-23,
EC-VAR-01, EC-VAR-08, EC-OT-09, EC-SEC-01, EC-SEC-02.

The helpers here are shared with test_run_approvals.py and test_run_immutability.py:

* ``calculate`` freezes an engine snapshot per included employee (built with the pure engine fixtures, so the
  amounts never depend on live sources), calculates it with the real engine and stores the snapshot, lines
  and figures, moving the run through ``runs.transition`` like the calculation service does; with
  ``store_outputs`` it also stores ``stage_outputs`` as the calculation service does, otherwise finalization
  replays the snapshot.
* ``prepare`` writes service-made results (like the payment-only and reversal services) for runs whose lines
  must reference real consumable records.
"""

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.helpers import create_employee
from tests.payroll.test_compensation import assign_and_approve, login_as
from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import (  # noqa: F401  (runs_world is a fixture)
    PAY,
    RUNS,
    current_period,
    force_status,
    new_run,
    principal_of,
    run_employees,
    runs_world,
)
from tests.payroll.test_wage_inputs import audit_actions, expect_error, set_setting, sql, tenant

ATTESTATIONS = f"{PAY}/statutory-attestations"
FINALIZED_TOPIC = "payroll.run.finalized"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def user_id_of(client):
    if getattr(client, "user_id", None) is None:
        client.user_id = client.me()["user"]["id"]
    return client.user_id


def step_up(client):
    sql("UPDATE user_sessions SET step_up_at = now(), step_up_method = 'EMAIL_OTP' WHERE user_id = :u "
        "AND revoked_at IS NULL", u=user_id_of(client))


def run_state(run_id):
    status, version, label = sql("SELECT status, row_version, label FROM payroll_runs WHERE id = :r", r=run_id)[0]
    return {"status": status, "row_version": version, "label": label}


def snapshot_data(run, row, *, kind="PLAIN", **overrides):
    """The frozen engine input of one run employee (pure fixtures; November 2026 example A structure)."""
    from app.payroll import packs
    from tests.payroll_unit.test_engine_fixtures import settings, snapshot

    data = snapshot(run={"run_id": run.id, "run_employee_id": row.id, "environment": run.environment},
                    employee={"employee_id": row.employee_id, "code": f"E{row.employee_id}", "hire_date": "2022-04-01",
                              "country": "IN", "subdivision": "IN-MH", "standard_hours_per_day": "8",
                              "protected": {"gender": "MALE"}},
                    currency=run.currency)
    if kind == "IN_FIXTURE":
        from app.payroll.packs.in_fixture import EPF, PT, TDS

        pack = packs.find_pack("IN", on=date(2026, 11, 30), environment="test")
        data["settings"] = settings(**{s.switch_key.replace(".", "__"): True for s in (EPF, PT, TDS)})
        data["pack"] = packs.pack_snapshot(pack)
        data["tax_profile"] = {"regime": "NEW"}
    elif kind == "ZZ":
        from app.payroll.packs.zz_test import LEVY, PENSION, WHT
        from tests.payroll_unit.test_engine_fixtures import catalogue, component

        pack = packs.find_pack("ZZ", environment="test")
        structure = {"id": 1, "structure_type": "GROSS_BASED", "currency": run.currency, "pay_basis_amount": "4000.00",
                     "lines": [{"component": "BASIC", "method": "FIXED", "amount": "4000.00"}]}
        data["employee"].update({"country": "ZZ", "subdivision": None, "protected": {}})
        data["components"] = catalogue(component("BASIC", tags=["LOP_APPLICABLE", "ZZ_PENSION_WAGE", "TAXABLE_INCOME"],
                                                 proration="BY_PAID_DAYS"))
        data["segments"] = [{"key": "S1", "start": "2026-11-01", "end": "2026-11-30", "compensation": structure}]
        data["settings"] = settings(**{s.switch_key.replace(".", "__"): True for s in (PENSION, LEVY, WHT)})
        data["pack"] = packs.pack_snapshot(pack)
        data["tax_profile"] = {"resident": True}
    data.update(overrides)
    return data


def _component_ids(db, company_id):
    from app.models.compensation import PayComponent

    return {c: i for c, i in db.query(PayComponent.code, PayComponent.id).filter(PayComponent.company_id == company_id)}


def _store(db, run, row, data, result, ids, *, store_outputs):
    from app.core import clock
    from app.models.pay_runs import PayrollInputSnapshot, PayrollLine
    from app.payroll.engine import snapshot as snap

    version = db.query(PayrollInputSnapshot).filter(PayrollInputSnapshot.run_employee_id == row.id).count() + 1
    frozen = PayrollInputSnapshot(company_id=run.company_id, run_employee_id=row.id, version=version, content=data,
                                  content_hash=snap.content_hash(data), source_hashes={})
    db.add(frozen)
    db.flush()
    for index, line in enumerate(result.lines):
        db.add(PayrollLine(
            company_id=run.company_id, run_id=run.id, run_employee_id=row.id, component_code=line.component,
            component_id=ids.get(line.component), kind=line.kind, category=line.category, stage=line.stage,
            amount=line.amount, currency=line.currency, units=line.units, rate=line.rate,
            tags=list(line.tags), visibility=line.visibility, sources=snap.to_plain(list(line.sources)),
            trace=snap.to_plain(line.trace), explanation=line.explanation, sort_order=line.sort_order or index,
            statutory=line.statutory, fixture=line.fixture))
    totals = result.totals
    row.snapshot_id, row.status = frozen.id, "CALCULATED"
    row.gross, row.deductions, row.reimbursements = totals.gross, totals.deductions, totals.reimbursements
    row.net, row.employer_cost = totals.net, totals.employer_cost
    row.content_hash, row.calc_hash, row.calculated_at = result.content_hash, result.calc_hash, clock.utcnow()
    if store_outputs:
        outputs = dict(result.stage_outputs or {})
        row.stage_outputs = {"engine_version": result.engine_version, "calc_hash": result.calc_hash,
                             "statutory": outputs.get("statutory") or {}, "deductions": outputs.get("deductions") or {},
                             "pack": data.get("pack")}
    row.row_version += 1
    db.flush()


def _set_totals(db, run, rows):
    from app.payroll.money import amount_str

    figures = {}
    for row in rows:
        if row.status != "CALCULATED":
            continue
        bucket = figures.setdefault(row.currency, {"net": Decimal(0), "gross": Decimal(0), "employees": 0})
        bucket["net"] += Decimal(row.net)
        bucket["gross"] += Decimal(row.gross)
        bucket["employees"] += 1
    run.totals = {cur: {"net": amount_str(f["net"], cur), "gross": amount_str(f["gross"], cur),
                        "employees": f["employees"]} for cur, f in figures.items()}


def calculate(client, run_id, *, kind="PLAIN", build=None, store_outputs=False):
    """READY... -> REVIEW_REQUIRED with engine results for every PENDING employee (calculation stand-in)."""
    from app.models.pay_runs import PayrollRunEmployee
    from app.payroll import engine
    from app.payroll.engine.snapshot import Snapshot
    from app.payroll.services import runs

    db, principal = principal_of(client)
    try:
        run = runs.load_run(db, principal.company_id, run_id)
        if run.status == "DRAFT":
            run = runs.transition(db, principal, run, "PRECHECK_PASSED")
        action = "CALCULATE" if run.status == "READY" else "RECALCULATE"
        run = runs.transition(db, principal, run, action, IDEMPOTENCY_KEY=True)
        rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id,
                                                   PayrollRunEmployee.status == "PENDING").all()
        ids = _component_ids(db, run.company_id)
        for row in rows:
            data = build(run, row) if build else snapshot_data(run, row, kind=kind)
            data = Snapshot.from_dict(data).to_dict()
            _store(db, run, row, data, engine.calculate(data), ids, store_outputs=store_outputs)
        _set_totals(db, run, db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id).all())
        db.flush()
        runs.transition(db, None, run, "COMPLETE_CALCULATION", system=True)
        runs.transition(db, None, run, "PASS_VALIDATION", system=True, NO_BLOCKING_CHECK_FAILED=True)
        db.commit()
    finally:
        db.close()


def line(code, amount, *, kind="EARNING", category="BASE", sources=(), statutory=False, fixture=False, units=None,
         trace=None):
    return {"component_code": code, "kind": kind, "category": category, "stage": 4 if kind == "EARNING" else 8,
            "amount": Decimal(amount), "currency": "INR", "units": units, "sources": list(sources),
            "trace": trace or {}, "statutory": statutory, "fixture": fixture}


def prepare(client, run_id, results=None):
    """Service-made results (no engine): ``results`` maps employee ids to line dicts; every other PENDING
    employee is paid BASIC 50,000.00."""
    from app.core import clock
    from app.models.pay_runs import PayrollLine, PayrollRunEmployee
    from app.payroll.services import runs

    db, principal = principal_of(client)
    try:
        run = runs.load_run(db, principal.company_id, run_id)
        if run.status == "DRAFT":
            run = runs.transition(db, principal, run, "PRECHECK_PASSED")
        action = "CALCULATE" if run.status == "READY" else "RECALCULATE"
        run = runs.transition(db, principal, run, action, IDEMPOTENCY_KEY=True)
        rows = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id,
                                                   PayrollRunEmployee.status == "PENDING").all()
        for row in rows:
            items = (results or {}).get(row.employee_id) or [line("BASIC", "50000.00")]
            earn = sum((i["amount"] for i in items if i["kind"] == "EARNING"), Decimal(0))
            deduct = sum((i["amount"] for i in items if i["kind"] == "DEDUCTION"), Decimal(0))
            reimb = sum((i["amount"] for i in items if i["kind"] == "REIMBURSEMENT"), Decimal(0))
            employer = sum((i["amount"] for i in items if i["kind"] == "EMPLOYER_CONTRIBUTION"), Decimal(0))
            for index, item in enumerate(items):
                db.add(PayrollLine(company_id=run.company_id, run_id=run.id, run_employee_id=row.id,
                                   sort_order=(index + 1) * 10, **{**item, "currency": run.currency}))
            row.status = "CALCULATED"
            row.gross, row.deductions, row.reimbursements = earn, deduct, reimb
            row.net, row.employer_cost = earn + reimb - deduct, earn + employer
            row.content_hash = row.calc_hash = f"{row.id:064x}"
            row.calculated_at = clock.utcnow()
            row.row_version += 1
        db.flush()
        _set_totals(db, run, db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run.id).all())
        db.flush()
        runs.transition(db, None, run, "COMPLETE_CALCULATION", system=True)
        runs.transition(db, None, run, "PASS_VALIDATION", system=True, NO_BLOCKING_CHECK_FAILED=True)
        db.commit()
    finally:
        db.close()


def submit(client, run_id, expect=200, **body):
    r = client.post(f"{RUNS}/{run_id}/submit", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def approve(client, run_id, expect=200, **body):
    body.setdefault("row_version", run_state(run_id)["row_version"])
    r = client.post(f"{RUNS}/{run_id}/approve", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def reject(client, run_id, reason="Wrong overtime", expect=200, **body):
    body.setdefault("row_version", run_state(run_id)["row_version"])
    r = client.post(f"{RUNS}/{run_id}/reject", json={"reason": reason, **body})
    assert r.status_code == expect, r.text
    return r.json()


def finalize(client, run_id, key="finalize-1", label=None, expect=200, **body):
    headers = {"Idempotency-Key": key} if key else {}
    r = client.post(f"{RUNS}/{run_id}/finalize", headers=headers,
                    json={"confirm_label": label if label is not None else run_state(run_id)["label"], **body})
    assert r.status_code == expect, r.text
    return r


def code_of(response):
    return (response if isinstance(response, dict) else response.json()).get("code")


def approved_run(w, *, group=None, period=None, prepared=None, **calc):
    """A run of the pay group (PG-IN by default) calculated (or prepared), submitted by the founder and
    approved by Ava."""
    group = group or w["group"]
    period = period or (w["period"] if group == w["group"] else current_period(w["acme"], group))
    run = new_run(w["acme"], group, period["id"])
    if prepared is not None:
        prepare(w["acme"], run["id"], prepared)
    else:
        calculate(w["acme"], run["id"], **calc)
    submit(w["acme"], run["id"])
    approve(w["approver"], run["id"])
    assert run_state(run["id"])["status"] == "APPROVED"
    return run


def outbox_count(company_id, run_id):
    return sql("SELECT count(*) FROM outbox_events WHERE company_id = :c AND topic = :t "
               "AND payload->>'run_id' = :r", c=company_id, t=FINALIZED_TOPIC, r=str(run_id))[0][0]


def ytd(company_id, employee_id, bucket):
    rows = sql("SELECT amount FROM ytd_balances WHERE company_id = :c AND employee_id = :e AND bucket = :b",
               c=company_id, e=employee_id, b=bucket)
    return rows[0][0] if rows else None


@pytest.fixture
def fin_world(app, runs_world, monkeypatch):
    """runs_world with a second approver (Bob) and the finalized-run outbox quiet: the event is recorded but the
    document, payment, journal and correction subscribers (later milestones, their own tests) do not run."""
    from app.payroll.services import outbox

    monkeypatch.setitem(outbox.SUBSCRIBERS, FINALIZED_TOPIC, ())
    w = runs_world
    w["bob"] = login_as(app, w["acme"], create_employee(w["acme"], "Bob", "Approver"), ["payroll_approver"])
    user_id_of(w["acme"])
    step_up(w["acme"])
    confirm_countries(w["cid"])
    return w


def confirm_countries(company_id):
    """Entities of a new tenant start with an unconfirmed country (EC-ORG-23 has its own test)."""
    sql("UPDATE legal_entities SET country_confirmed = true WHERE company_id = :c", c=company_id)


# ---------------------------------------------------------------------------
# Full lifecycle (EC-RUN-04, EC-STA-22, EC-STA-10)
# ---------------------------------------------------------------------------
def test_ec_run_04_full_lifecycle_finalizes_once_with_ytd_liabilities_and_one_outbox_event(fin_world):
    w = fin_world
    acme, cid = w["acme"], w["cid"]
    run = approved_run(w, kind="IN_FIXTURE")
    emma = w["emp"]["Emma"]["id"]
    status = ok(acme.get(f"{RUNS}/{run['id']}/finalization"))
    assert status["can_finalize"] is True and status["blockers"] == [] and status["statutory"]["verified"]

    first = finalize(acme, run["id"])
    body = first.json()
    assert body["status"] == "FINALIZED" and body["finalized_at"] and body["outbox_event_id"]
    stored = sql("SELECT status, finalized_by_user_id, finalized_at IS NOT NULL FROM payroll_runs WHERE id = :r",
                 r=run["id"])[0]
    assert stored == ("FINALIZED", w["acme"].user_id, True)
    assert sql("SELECT status FROM pay_periods WHERE id = :p", p=w["period"]["id"])[0][0] == "CLOSED"

    net, gross = sql("SELECT net, gross FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e",
                     r=run["id"], e=emma)[0]
    assert ytd(cid, emma, "TOTAL:NET") == net and ytd(cid, emma, "TOTAL:GROSS") == gross
    basic = sql("SELECT sum(l.amount) FROM payroll_lines l JOIN payroll_run_employees e ON e.id = l.run_employee_id "
                "WHERE e.run_id = :r AND e.employee_id = :e AND l.component_code = 'BASIC'", r=run["id"], e=emma)[0][0]
    assert ytd(cid, emma, "BASIC") == basic
    # Statutory fixture lines (IN fixture pack, test environment): liabilities per scheme, fixture kept (EC-STA-10).
    liabilities = sql("SELECT scheme, employee_amount, employer_amount, fixture FROM statutory_liabilities "
                      "WHERE run_id = :r ORDER BY scheme", r=run["id"])
    assert liabilities and all(row[3] for row in liabilities)
    assert ("IN.pt", Decimal("400.00"), Decimal("0.00"), True) in liabilities  # PT of Emma and Ivan, one scheme row
    # No stored outputs: finalization replayed the frozen snapshot and kept the outputs for the next snapshot.
    replayed = sql("SELECT stage_outputs->>'replayed', stage_outputs->>'calc_hash' = calc_hash "
                   "FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED'", r=run["id"])
    assert replayed and all(r == ("true", True) for r in replayed)
    assert outbox_count(cid, run["id"]) == 1
    actions = audit_actions(cid, "payroll_run", run["id"])
    assert {"RUN_SUBMITTED", "RUN_STEP_APPROVED", "RUN_FINALIZED"} <= set(actions)
    details = sql("SELECT details FROM audit_logs WHERE company_id = :c AND entity_id = :r AND action = 'RUN_FINALIZED'",
                  c=cid, r=str(run["id"]))[0][0]
    assert details["effects"]["employees"] == 2 and details["effects"]["liabilities"] == len(liabilities)

    # EC-RUN-04: a retry after a timeout replays; nothing happens twice.
    again = finalize(acme, run["id"])
    assert again.json() == body and again.headers.get("Idempotent-Replayed") == "true"
    assert outbox_count(cid, run["id"]) == 1 and ytd(cid, emma, "TOTAL:NET") == net
    r = finalize(acme, run["id"], key="finalize-other", expect=409)
    assert code_of(r) == "RUN_FINALIZED" and r.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION"
    status = ok(acme.get(f"{RUNS}/{run['id']}/finalization"))
    assert status["can_finalize"] is False and status["finalized_at"]


def test_finalize_needs_step_up_typed_label_idempotency_key_permission_and_scope(fin_world):
    w = fin_world
    acme = w["acme"]
    run = approved_run(w)
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=acme.user_id)
    r = finalize(acme, run["id"], expect=403)
    assert code_of(r) == "STEP_UP_REQUIRED"
    step_up(acme)
    r = finalize(acme, run["id"], label="Some other run", expect=400)
    assert code_of(r) == "CONFIRMATION_MISMATCH"
    r = finalize(acme, run["id"], key=None, expect=400)
    assert code_of(r) == "IDEMPOTENCY_KEY_REQUIRED"
    r = acme.post(f"{RUNS}/{run['id']}/finalize", json={"confirm_label": run_state(run["id"])["label"],
                                                        "status": "FINALIZED"}, headers={"Idempotency-Key": "x"})
    assert r.status_code == 422  # server-owned fields are refused (EC-SEC-06)
    assert finalize(w["approver"], run["id"], expect=403)  # payroll:run:finalize is not an approver's right
    assert finalize(w["processor"], run["id"], expect=403)
    assert code_of(finalize(w["cleo"], run["id"], expect=404)) == "NOT_FOUND"  # AE01 clerk: out of scope
    assert code_of(finalize(w["beta"]["client"], run["id"], expect=404)) == "NOT_FOUND"
    assert run_state(run["id"])["status"] == "APPROVED"
    finalize(acme, run["id"], key="finalize-ok")
    assert run_state(run["id"])["status"] == "FINALIZED"


def test_ec_run_09_stale_run_cannot_be_submitted_approved_or_finalized(fin_world):
    from app.payroll.services import runs

    w = fin_world
    acme, cid = w["acme"], w["cid"]

    def mark_stale():
        db, _ = principal_of(acme)
        try:
            assert runs.mark_stale(db, cid, "A compensation change affects locked inputs.",
                                   employee_ids=[w["emp"]["Emma"]["id"]]) == 1
            db.commit()
        finally:
            db.close()

    run = new_run(acme, w["group"], w["period"]["id"])
    prepare(acme, run["id"])
    mark_stale()
    r = acme.post(f"{RUNS}/{run['id']}/submit", json={})
    assert r.status_code == 409 and code_of(r) == "RUN_STALE"
    sql("UPDATE payroll_runs SET stale = false, stale_reasons = '[]' WHERE id = :r", r=run["id"])
    submit(acme, run["id"])
    mark_stale()
    r = approve(w["approver"], run["id"], expect=409)
    assert code_of(r) == "RUN_STALE"
    sql("UPDATE payroll_runs SET stale = false, stale_reasons = '[]' WHERE id = :r", r=run["id"])
    approve(w["approver"], run["id"])
    mark_stale()
    r = finalize(acme, run["id"], expect=409)
    assert code_of(r) == "RUN_STALE" and run_state(run["id"])["status"] == "APPROVED"
    assert "Inputs changed after lock" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Statutory gate (EC-STA-01/02/03, I-07, I-15, EC-ORG-23)
# ---------------------------------------------------------------------------
def zz_entity(w):
    """ZZ01 (Zedland, the fictional verified test pack) with pay group PG-ZZ (EUR) and Zoe, salaried."""
    from tests.payroll.test_runs_lifecycle import calendar_for
    from tests.payroll.test_wage_inputs import pay_group, place

    acme, cid = w["acme"], w["cid"]
    r = acme.post("/api/v1/legal-entities", json={"code": "ZZ01", "legal_name": "Zedland Ltd", "country_code": "DE",
                                                   "base_currency": "EUR", "timezone": "Europe/Berlin"})
    assert r.status_code == 200, r.text
    entity = r.json()["id"]
    r = acme.post("/api/v1/branches", json={"legal_entity_id": entity, "code": "ZED", "name": "Zed City"})
    assert r.status_code == 200, r.text
    branch = r.json()["id"]
    sql("UPDATE legal_entities SET country_code = 'ZZ' WHERE id = :e", e=entity)
    sql("UPDATE branches SET country_code = 'ZZ' WHERE id = :b", b=branch)
    group = pay_group(cid, entity, "PG-ZZ", currency="EUR")
    zoe = create_employee(acme, "Zoe", hire_date="2024-01-15")
    r = acme.post(f"/api/v1/employees/{zoe['id']}/assignments",
                  json={"branch_id": branch, "effective_from": "2024-01-16", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    place(zoe["id"], entity, group)
    calendar_for(acme, group)
    period = current_period(acme, group)
    assign_and_approve(acme, w["approver"], zoe["id"], w["version"], "1200000",
                       effective_from=date.fromisoformat(period["start"]), currency="EUR")
    confirm_countries(cid)
    return {"entity": entity, "group": group, "period": period, "zoe": zoe["id"]}


def test_ec_sta_02_verified_zz_pack_finalizes_statutory_lines_with_liabilities_and_pack_state(fin_world):
    w = fin_world
    acme, cid = w["acme"], w["cid"]
    zz = zz_entity(w)
    run = new_run(acme, zz["group"], zz["period"]["id"])
    calculate(acme, run["id"], kind="ZZ", store_outputs=True)
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    lines = dict(sql("SELECT component_code, amount FROM payroll_lines WHERE run_id = :r AND statutory "
                     "AND NOT fixture", r=run["id"]))
    assert lines["ZZ_PENSION_EE"] == Decimal("200.00") and lines["ZZ_WHT"] == Decimal("752.00")
    # EC-STA-03: a pack that changed after calculation makes the run stale.
    sql("UPDATE payroll_run_employees SET stage_outputs = jsonb_set(stage_outputs, '{pack,fingerprint}', "
        "'\"tampered\"') WHERE run_id = :r", r=run["id"])
    r = finalize(acme, run["id"], expect=409)
    assert code_of(r) == "RUN_STALE" and "country pack changed" in r.json()["detail"]
    from app.payroll import packs

    sql("UPDATE payroll_run_employees SET stage_outputs = jsonb_set(stage_outputs, '{pack,fingerprint}', "
        "to_jsonb(CAST(:f AS text))) WHERE run_id = :r", r=run["id"],
        f=packs.find_pack("ZZ", environment="test").fingerprint())
    finalize(acme, run["id"], key="zz-1")
    liabilities = {s: (e, er, f) for s, e, er, f in sql(
        "SELECT scheme, employee_amount, employer_amount, fixture FROM statutory_liabilities WHERE run_id = :r",
        r=run["id"])}
    assert liabilities and not any(f for _, _, f in liabilities.values())
    pension = next(v for s, v in liabilities.items() if "pension" in s.lower())
    assert pension[0] == Decimal("200.00") and pension[1] == Decimal("280.00")  # EC-STA-10
    assert ytd(cid, zz["zoe"], "TOTAL:NET") == Decimal("3008.00")
    from app.payroll.services import finalization

    db, _ = principal_of(acme)
    try:
        carried = finalization.carried_state(db, cid, zz["zoe"], zz["entity"], date(2099, 1, 1))
        assert carried["run_id"] == run["id"] and isinstance(carried["pack_state"], dict)
        snapshot_ytd = finalization.ytd_for_snapshot(db, cid, zz["zoe"], zz["entity"], zz["period"]["tax_year"], "EUR")
        assert snapshot_ytd["TOTAL:NET"] == "3008.00"
    finally:
        db.close()


def test_ec_sta_02_i15_ae_draft_pack_needs_an_active_maker_checker_attestation(app, fin_world):
    w = fin_world
    acme = w["acme"]
    ae_period = current_period(acme, w["ae_group"])
    assign_and_approve(acme, w["approver"], w["gus"]["id"], w["version"], "1200000",
                       effective_from=date.fromisoformat(ae_period["start"]), currency="AED")
    run = approved_run(w, group=w["ae_group"], period=ae_period, prepared={})
    r = finalize(acme, run["id"], expect=409)
    assert code_of(r) == "STATUTORY_NOT_VERIFIED"
    attestation = ok(acme.post(ATTESTATIONS, json={"legal_entity_id": w["ae"], "effective_from": "2020-01-01",
                                                   "reason": "Handled by our UAE provider"}), 201)
    assert acme.post(f"{ATTESTATIONS}/{attestation['id']}/approve").status_code == 403  # maker != checker
    assert code_of(finalize(acme, run["id"], expect=409)) == "STATUTORY_NOT_VERIFIED"
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    # An attestation covers obligations handled outside the system, never statutory lines of an unverified pack.
    row = sql("SELECT id FROM payroll_run_employees WHERE run_id = :r AND status = 'CALCULATED'", r=run["id"])[0][0]
    sql("INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, amount, "
        "currency, tags, visibility, sources, trace, sort_order, statutory, fixture, created_at, updated_at) "
        "VALUES (:c, :r, :e, 'AE_GPSSA_EE', 'DEDUCTION', 6, 100, 'AED', '[]', 'EMPLOYEE_VISIBLE', '[]', '{}', 99, "
        "true, false, now(), now())", c=w["cid"], r=run["id"], e=row)
    assert code_of(finalize(acme, run["id"], expect=409)) == "STATUTORY_NOT_VERIFIED"
    sql("DELETE FROM payroll_lines WHERE run_id = :r AND component_code = 'AE_GPSSA_EE'", r=run["id"])
    # EC-ORG-23: an unconfirmed entity country blocks too.
    sql("UPDATE legal_entities SET country_confirmed = false WHERE id = :e", e=w["ae"])
    assert code_of(finalize(acme, run["id"], expect=409)) == "UNCONFIRMED_COUNTRY"
    sql("UPDATE legal_entities SET country_confirmed = true WHERE id = :e", e=w["ae"])
    finalize(acme, run["id"], key="ae-1")
    assert run_state(run["id"])["status"] == "FINALIZED"
    assert sql("SELECT count(*) FROM statutory_liabilities WHERE run_id = :r", r=run["id"])[0][0] == 0


def test_i07_fixture_statutory_lines_never_finalize_in_production(fin_world):
    w = fin_world
    run = approved_run(w, kind="IN_FIXTURE")
    sql("UPDATE payroll_runs SET environment = 'production' WHERE id = :r", r=run["id"])
    r = finalize(w["acme"], run["id"], expect=409)
    assert code_of(r) == "STATUTORY_NOT_VERIFIED"  # the IN fixture pack is refused in production
    assert run_state(run["id"])["status"] == "APPROVED"
    assert sql("SELECT count(*) FROM ytd_balances WHERE company_id = :c", c=w["cid"])[0][0] == 0


# ---------------------------------------------------------------------------
# Consumption exactly once (EC-VAR-01, EC-VAR-08, EC-OT-09)
# ---------------------------------------------------------------------------
def _wage_input(w, employee_id, amount="5000.00"):
    period = w["period"]
    component = sql("SELECT id FROM pay_components WHERE company_id = :c AND code = 'BASIC'", c=w["cid"])[0][0]
    return sql("INSERT INTO wage_inputs (company_id, employee_id, legal_entity_id, pay_group_id, component_id, "
               "component_code, amount, currency, target_period_start, target_period_end, attachments, source, status, "
               "flags, row_version, decided_at, created_at, updated_at) VALUES (:c, :e, :le, :pg, :comp, 'BONUS', :a, "
               "'INR', :s, :t, '[]', 'MANUAL', 'APPROVED', '[]', 1, now(), now(), now()) RETURNING id",
               c=w["cid"], e=employee_id, le=w["le01"], pg=w["group"], comp=component, a=amount,
               s=period["start"], t=period["end"])[0][0]


def _overtime(w, employee_id, day, minutes=120, comp_off=False):
    return sql("INSERT INTO overtime_entries (company_id, employee_id, date, minutes_requested, minutes_approved, "
               "source, status, comp_off, pre_approval, version, decided_at, row_version, created_at, updated_at) "
               "VALUES (:c, :e, :d, :m, :m, 'APPROVED_REQUESTS', 'APPROVED', :co, false, 1, now(), 1, now(), now()) "
               "RETURNING id", c=w["cid"], e=employee_id, d=day, m=minutes, co=comp_off)[0][0]


def _task(w, employee_id):
    task = sql("INSERT INTO tasks (company_id, title, task_type, quantity, unit, status, approved_on, decided_at, "
               "row_version, created_at, updated_at) VALUES (:c, 'Install meters', 'INSTALL', 2, 'unit', 'APPROVED', "
               ":d, now(), 1, now(), now()) RETURNING id", c=w["cid"], d=w["period"]["start"])[0][0]
    sql("INSERT INTO task_assignees (company_id, task_id, employee_id, split_percent, created_at, updated_at) "
        "VALUES (:c, :t, :e, 100, now(), now())", c=w["cid"], t=task, e=employee_id)
    return task


def _claim(w, employee_id, amount="300.00"):
    policy = sql("SELECT id FROM policy_versions WHERE company_id = :c ORDER BY id LIMIT 1", c=w["cid"])[0][0]
    return sql("INSERT INTO reimbursement_claims (company_id, employee_id, legal_entity_id, pay_group_id, "
               "policy_version_id, claim_type, claim_type_name, taxability, expense_date, amount, currency, "
               "payout_route, payout_amount, payout_currency, approved_amount, status, flags, decided_at, row_version, "
               "created_at, updated_at) VALUES (:c, :e, :le, :pg, :p, 'TRAVEL', 'Travel', 'EXEMPT', :d, :a, "
               "'INR', 'WITH_PAYROLL', :a, 'INR', :a, 'APPROVED', '[]', now(), 1, now(), now()) RETURNING id",
               c=w["cid"], e=employee_id, le=w["le01"], pg=w["group"], p=policy, d=w["period"]["start"],
               a=amount)[0][0]


def _loan(app, w, employee_id):
    from app.payroll.services import loans
    from tests.payroll.test_loans import ADVANCE_POLICY, GROSS, LOAN_POLICY, NET, STAFF, disburse, pay_figures
    from tests.payroll.test_wage_inputs import activate_policy

    set_setting(w["cid"], "payroll.loans.enabled", True)
    set_setting(w["cid"], "payroll.advances.enabled", True)
    set_setting(w["cid"], "payroll.deductions.min_net_pay_percent", "60")
    activate_policy(w["acme"], "LOAN_POLICY", LOAN_POLICY)
    activate_policy(w["acme"], "ADVANCE_POLICY", ADVANCE_POLICY)
    previous = loans.set_pay_figures_provider(lambda db, c, e, on, currency: pay_figures(GROSS, NET))
    try:
        r = w["processor"].post(STAFF, json={"employee_id": employee_id, "kind": "LOAN",
                                             "amount": {"amount": "60000.00", "currency": "INR"}, "tenure": 6})
        assert r.status_code == 201, r.text
        loan = r.json()["id"]
        assert w["acme"].post(f"{STAFF}/{loan}/approve", json={}).status_code == 200
        assert disburse(w["cid"], loan, reference=f"PAY-{loan}") == "ACTIVE"
    finally:
        loans.set_pay_figures_provider(previous)
    return loan


def test_ec_var_01_ec_var_08_sources_are_consumed_exactly_once(app, fin_world):
    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    bonus = _wage_input(w, emma)
    spare = _wage_input(w, ivan, "700.00")       # reserved for Ivan but not paid by a line: released
    day = date.fromisoformat(w["period"]["start"])
    entry = _overtime(w, emma, day)
    task = _task(w, emma)
    claim = _claim(w, emma)
    loan = _loan(app, w, emma)
    run = new_run(acme, w["group"], w["period"]["id"])
    ivan_row = run_employees(run["id"])[ivan][2]
    sql("UPDATE wage_inputs SET reserved_by_run_employee_id = :r WHERE id = :i", r=ivan_row, i=spare)
    prepare(acme, run["id"], {emma: [
        line("BASIC", "50000.00"),
        line("BONUS", "5000.00", category="BONUS", sources=[{"type": "wage_input", "id": bonus}]),
        line("OT_WORKDAY", "721.15", category="OVERTIME",
             sources=[{"type": "overtime_entry", "id": f"overtime:{entry}", "minutes": "120"}]),
        line("TASK_EARNING", "400.00", category="TASK_EARNING", sources=[{"type": "task", "id": f"task:{task}"}]),
        line("REIMBURSEMENT", "300.00", kind="REIMBURSEMENT", category="REIMBURSEMENT",
             sources=[{"type": "claim", "id": claim, "claim_type": "TRAVEL"}]),
        line("LOAN_RECOVERY", "10000.00", kind="DEDUCTION", category="LOAN_REPAYMENT",
             sources=[{"type": "loan", "id": loan, "instalment": "1"}], trace={"due": "10000.00"}),
    ]})
    submit(acme, run["id"])
    approve(w["approver"], run["id"])
    finalize(acme, run["id"], key="consume-1")
    emma_row = run_employees(run["id"])[emma][2]
    assert sql("SELECT status, consumed_by_run_employee_id FROM wage_inputs WHERE id = :i", i=bonus)[0] == (
        "CONSUMED", emma_row)
    assert sql("SELECT status, reserved_by_run_employee_id FROM wage_inputs WHERE id = :i", i=spare)[0] == (
        "APPROVED", None)
    assert sql("SELECT consumed_by_run_employee_id FROM overtime_entries WHERE id = :i", i=entry)[0][0] == emma_row
    assert sql("SELECT status, paid_in_run_id FROM tasks WHERE id = :t", t=task)[0] == ("PAID", run["id"])
    assert sql("SELECT status, paid_ref FROM reimbursement_claims WHERE id = :c", c=claim)[0] == (
        "PAID", f"run:{run['number']}")
    instalments = sql("SELECT amount FROM loan_transactions WHERE loan_id = :l AND kind = 'INSTALMENT'", l=loan)
    assert instalments == [(Decimal("10000.0000"),)]
    consumed = dict(sql("SELECT source_type, count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed "
                        "GROUP BY source_type", r=run["id"]))
    assert consumed == {"WAGE_INPUT": 1, "OVERTIME": 1, "TASK": 1, "CLAIM": 1, "LOAN": 1}
    # A retry replays; nothing is consumed or recovered twice (EC-RUN-04, EC-VAR-08).
    again = finalize(acme, run["id"], key="consume-1")
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert len(sql("SELECT id FROM loan_transactions WHERE loan_id = :l AND kind = 'INSTALMENT'", l=loan)) == 1

    # EC-VAR-01: another run paying the same wage input is refused; nothing of it is consumed.
    off = new_run(acme, w["group"], w["period"]["id"], run_type="OFF_CYCLE", employee_ids=[emma])
    prepare(acme, off["id"], {emma: [line("BONUS", "5000.00", category="BONUS",
                                          sources=[{"type": "wage_input", "id": bonus}])]})
    submit(acme, off["id"])
    approve(w["approver"], off["id"])
    r = finalize(acme, off["id"], key="consume-2", expect=409)
    assert code_of(r) == "SOURCE_ALREADY_CONSUMED"
    assert run_state(off["id"])["status"] == "APPROVED"
    assert sql("SELECT count(*) FROM payroll_line_sources WHERE run_id = :r AND consumed", r=off["id"])[0][0] == 0


def test_ec_ot_09_comp_off_is_credited_to_the_comp_off_balance_once(fin_world):
    from tests.payroll_unit.test_engine_fixtures import overtime_policy, settings

    w = fin_world
    acme, cid = w["acme"], w["cid"]
    emma = w["emp"]["Emma"]["id"]
    paid = _overtime(w, emma, date.fromisoformat(w["period"]["start"]))
    rest = _overtime(w, emma, date.fromisoformat(w["period"]["start"]), comp_off=True)

    def build(run, row):
        extra = {}
        if row.employee_id == emma:
            extra = {"settings": settings(payroll__overtime__enabled=True),
                     "policies": {"OVERTIME_POLICY": overtime_policy(comp_off_allowed=True, comp_off_ratio="1.5")},
                     "overtime_entries": [{"entry_id": f"overtime:{paid}", "date": "2026-11-05", "minutes": 120},
                                          {"entry_id": f"overtime:{rest}", "date": "2026-11-06", "minutes": 120,
                                           "comp_off": True}]}
        return snapshot_data(run, row, **extra)

    run = approved_run(w, build=build)
    r = finalize(acme, run["id"], expect=409)
    assert code_of(r) == "COMP_OFF_TYPE_MISSING"   # never silently dropped
    sql("INSERT INTO leave_types (company_id, code, name, annual_quota, is_paid, is_unlimited, is_active, "
        "counts_non_working_days, allow_half_day, kind, created_at, updated_at) VALUES (:c, 'COMP', 'Comp-off', 0, "
        "true, false, true, false, true, 'COMP_OFF', now(), now())", c=cid)
    finalize(acme, run["id"], key="comp-off-1")
    emma_row = run_employees(run["id"])[emma][2]
    assert sql("SELECT consumed_by_run_employee_id FROM overtime_entries WHERE id IN (:a, :b) ORDER BY id",
               a=paid, b=rest) == [(emma_row,), (emma_row,)]
    credit = sql("SELECT leave_type, delta, reason, idempotency_key FROM leave_balance_transactions "
                 "WHERE company_id = :c AND employee_id = :e", c=cid, e=emma)
    assert credit == [("COMP", Decimal("0.38"), "ACCRUAL", f"payroll:comp_off:{rest}")]  # 2 h × 1.5 ÷ 8 h


# ---------------------------------------------------------------------------
# Close (3.5 §5.10) and holds carried (EC-RUN-13)
# ---------------------------------------------------------------------------
def test_close_reconciled_run_carries_payment_holds_and_refuses_open_payments(fin_world):
    w = fin_world
    acme = w["acme"]
    run = approved_run(w, prepared={})
    ivan_row = run_employees(run["id"])[w["emp"]["Ivan"]["id"]][2]
    r = acme.post(f"{RUNS}/{run['id']}/close", json={})
    assert r.status_code == 409 and code_of(r) == "INVALID_TRANSITION"  # APPROVED cannot close
    r = acme.post(f"{RUNS}/{run['id']}/employees/{ivan_row}/hold",
                  json={"hold_type": "PAYMENT", "reason_code": "BANK_ISSUE", "note": "Account closed"})
    assert r.status_code == 200, r.text
    finalize(acme, run["id"], key="close-1")
    r = acme.post(f"{RUNS}/{run['id']}/close", json={})
    assert r.status_code == 409 and code_of(r) == "PAYMENTS_OPEN"  # Emma still has to be paid
    assert w["processor"].post(f"{RUNS}/{run['id']}/close", json={}).status_code == 403
    assert w["beta"]["client"].post(f"{RUNS}/{run['id']}/close", json={}).status_code == 404
    force_status(run["id"], "RECONCILED")   # payments drive FINALIZED -> ... -> RECONCILED (their milestone)
    closed = ok(acme.post(f"{RUNS}/{run['id']}/close", json={"note": "September closed"}))
    assert closed["status"] == "CLOSED"
    assert closed["carried_holds"] == [{"run_employee_id": ivan_row, "employee_id": w["emp"]["Ivan"]["id"],
                                        "reason_code": "BANK_ISSUE"}]
    assert "RUN_CLOSED" in audit_actions(w["cid"], "payroll_run", run["id"])
    r = acme.post(f"{RUNS}/{run['id']}/close", json={})
    assert r.status_code == 409 and code_of(r) == "RUN_FINALIZED"


def test_finalized_run_with_nothing_to_pay_closes_directly(fin_world):
    w = fin_world
    acme = w["acme"]
    run = approved_run(w, prepared={e["id"]: [line("BASIC", "0.00")] for e in w["emp"].values()})
    finalize(acme, run["id"], key="zero-1")
    assert ok(acme.post(f"{RUNS}/{run['id']}/close", json={}))["status"] == "CLOSED"


# ---------------------------------------------------------------------------
# Isolation (EC-SEC-01, EC-SEC-02)
# ---------------------------------------------------------------------------
def test_ec_sec_01_finalization_writes_are_isolated_across_three_tenants(fin_world):
    w = fin_world
    run = approved_run(w, kind="IN_FIXTURE")
    finalize(w["acme"], run["id"], key="iso-1")
    for other in ("beta", "cobalt"):
        client = w[other]["client"]
        assert client.get(f"{RUNS}/{run['id']}/finalization").status_code == 404
        assert client.get(f"{RUNS}/{run['id']}/approvals").status_code == 404
        with tenant(w[other]["cid"]) as conn:
            for table in ("ytd_balances", "statutory_liabilities", "payroll_approvals", "payroll_line_sources",
                          "payroll_input_snapshots"):
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
            updated = conn.execute(text("UPDATE ytd_balances SET amount = 0 WHERE company_id = :c"),
                                   {"c": w["cid"]}).rowcount
            assert updated == 0
            expect_error(conn, "42501", "INSERT INTO ytd_balances (company_id, employee_id, legal_entity_id, tax_year, "
                                        "bucket, amount, currency, row_version, created_at, updated_at) VALUES "
                                        "(:c, :e, :le, '2026', 'X', 1, 'INR', 1, now(), now())",
                         c=w["cid"], e=w["emp"]["Emma"]["id"], le=w["le01"])
    with tenant(w["cid"]) as conn:
        assert conn.execute(text("SELECT count(*) FROM ytd_balances")).scalar() > 0
    with tenant(None) as conn:
        assert conn.execute(text("SELECT count(*) FROM ytd_balances")).scalar() == 0


def test_ec_run_08_concurrent_finalize_with_two_keys_finalizes_once(app, fin_world):
    from tests.helpers import login

    w = fin_world
    run = approved_run(w)
    clients = [login(app, w["acme"].email) for _ in range(2)]
    for client in clients:
        client.user_id = w["acme"].user_id
    step_up(w["acme"])
    barrier = threading.Barrier(2)
    results = []
    label = run_state(run["id"])["label"]

    def attempt(client, key):
        barrier.wait()
        r = client.post(f"{RUNS}/{run['id']}/finalize", json={"confirm_label": label},
                        headers={"Idempotency-Key": key})
        results.append((r.status_code, r.json().get("code")))

    threads = [threading.Thread(target=attempt, args=(c, f"race-{i}")) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(s for s, _ in results) == [200, 409], results
    assert [c for s, c in results if s == 409] == ["RUN_FINALIZED"]
    assert outbox_count(w["cid"], run["id"]) == 1
    assert audit_actions(w["cid"], "payroll_run", run["id"]).count("RUN_FINALIZED") == 1


# ---------------------------------------------------------------------------
# End to end through the real services (no stand-ins, every subscriber of payroll.run.finalized)
# ---------------------------------------------------------------------------
def test_ec_run_04_i15_real_chain_calculate_review_approve_finalize_drains_into_obligations_and_payslips(
        app, runs_world):
    """create -> precheck -> calculate job (the real snapshot builder and engine) -> review -> submit ->
    approve by another user -> finalize with step-up (entity without a country pack: an ACTIVE maker-checker
    attestation, I-15) -> the ONE payroll.run.finalized event is dispatched to its real subscribers -> payment
    obligations equal the finalized nets and payslips carry the finalized figures. A retry replays."""
    from app.payroll.services import outbox
    from tests.payroll.test_payment_obligations import NUMBERS, add_bank_account
    from tests.payroll.test_run_calculation import calculate as calculate_job
    from tests.payroll.test_run_calculation import no_pack_country, precheck_ready

    w = runs_world
    acme, cid = w["acme"], w["cid"]
    assert outbox.subscribers(FINALIZED_TOPIC), "the payments, documents, journals and corrections subscribers"
    no_pack_country(w)
    set_setting(cid, "payroll.attendance.enabled", False)
    confirm_countries(cid)
    user_id_of(acme)
    emma, ivan = w["emp"]["Emma"]["id"], w["emp"]["Ivan"]["id"]
    for name in ("Emma", "Ivan"):
        add_bank_account(cid, w["emp"][name]["id"], NUMBERS[name], holder=f"{name} Test")

    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    assert calculate_job(acme, run["id"])["action"] == "CALCULATE"
    assert run_state(run["id"])["status"] == "REVIEW_REQUIRED"
    rows = {e: (rid, status, net) for e, rid, status, net in sql(
        "SELECT employee_id, id, status, net FROM payroll_run_employees WHERE run_id = :r", r=run["id"])}
    assert rows[emma][1] == rows[ivan][1] == "CALCULATED" and rows[emma][2] == Decimal("100000")
    assert sql("SELECT count(*) FROM payroll_input_snapshots s JOIN payroll_run_employees e "
               "ON e.id = s.run_employee_id WHERE e.run_id = :r", r=run["id"])[0][0] >= 2

    # Review: the summary and the variance list; every flag is acknowledged before submission.
    assert ok(acme.get(f"{RUNS}/{run['id']}/summary"))
    for item in ok(acme.get(f"{RUNS}/{run['id']}/variance", params={"only_flagged": True}))["items"]:
        if item["pending"]:
            ok(acme.post(f"{RUNS}/{run['id']}/variance/{item['run_employee_id']}/acknowledge",
                         json={"comment": "Expected: first run of the year"}))
    submit(acme, run["id"])
    assert code_of(approve(acme, run["id"], expect=403)) == "FORBIDDEN"   # the preparer holds no approve right
    approve(w["approver"], run["id"])
    assert run_state(run["id"])["status"] == "APPROVED"

    # Finalize: step-up first (403), then the statutory gate (no pack for the entity: I-15), then the effects.
    sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id = :u", u=acme.user_id)
    assert code_of(finalize(acme, run["id"], key="e2e-1", expect=403)) == "STEP_UP_REQUIRED"
    step_up(acme)
    assert code_of(finalize(acme, run["id"], key="e2e-2", expect=409)) == "STATUTORY_NOT_VERIFIED"
    attestation = ok(acme.post(ATTESTATIONS, json={"legal_entity_id": w["le01"], "effective_from": "2020-01-01",
                                                   "reason": "Statutory filings by our external provider"}), 201)
    checker = login_as(app, acme, create_employee(acme, "Chen", "Checker"), ["payroll_manager"])
    ok(checker.post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
    body = finalize(acme, run["id"], key="e2e-3").json()
    assert body["status"] == "FINALIZED" and body["outbox_event_id"]

    assert outbox_count(cid, run["id"]) == 1
    dispatched = sql("SELECT dispatched_at IS NOT NULL, last_error FROM outbox_events WHERE id = :i",
                     i=body["outbox_event_id"])
    assert dispatched == [(True, None)]
    handled = dict(sql("SELECT type, status FROM jobs WHERE company_id = :c AND (payload->>'run_id')::int = :r "
                       "AND idempotency_key LIKE :k", c=cid, r=run["id"], k=f"outbox:{body['outbox_event_id']}:%"))
    assert set(handled) == set(outbox.subscribers(FINALIZED_TOPIC)) and set(handled.values()) == {"SUCCEEDED"}, handled
    nets = {e: n for e, n in sql("SELECT employee_id, net FROM payroll_run_employees WHERE run_id = :r "
                                 "AND status = 'CALCULATED' AND net > 0", r=run["id"])}
    assert set(nets) == {emma, ivan}
    owed = {e: a for e, a in sql("SELECT employee_id, amount FROM payment_obligations WHERE run_id = :r", r=run["id"])}
    assert owed == nets
    payslips = sql("SELECT d.run_employee_id, d.content->'totals'->>'net' FROM documents d WHERE d.run_id = :r "
                   "AND d.doc_type = 'PAYSLIP_V2' ORDER BY d.run_employee_id", r=run["id"])
    assert payslips == sorted((rows[e][0], f"{nets[e]:.2f}") for e in (emma, ivan))
    assert ytd(cid, emma, "TOTAL:NET") == nets[emma]

    # EC-RUN-04: the retry replays; no second event, obligation or payslip.
    again = finalize(acme, run["id"], key="e2e-3")
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json() == body
    assert outbox_count(cid, run["id"]) == 1
    assert sql("SELECT count(*) FROM payment_obligations WHERE run_id = :r", r=run["id"])[0][0] == 2
    assert sql("SELECT count(*) FROM documents WHERE run_id = :r AND doc_type = 'PAYSLIP_V2'", r=run["id"])[0][0] == 2
    assert ytd(cid, emma, "TOTAL:NET") == nets[emma]
