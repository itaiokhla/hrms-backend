"""The snapshot builder: the frozen calculation input of one run employee (3.5 §5.3, §8.1; FOLLOW-UPS F-02..F-04,
F-14, F-15, F-17, F-36).

A snapshot of a real seeded employee passes the engine's strict schema and calculates; building twice gives the
same content hash; segments split on revisions and transfers (EC-CMP-16, EC-ORG-06); YTD is per legal entity and
tax year (EC-STA-08, EC-STA-22); the previous finalized period feeds variance and the pack state (EC-STA-17);
inputs are reserved at lock; locked snapshots are immutable versions; tenants never see each other's snapshots.
"""

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.payroll.test_compensation import assign_and_approve
from tests.payroll.test_runs_lifecycle import (  # noqa: F401 - runs_world is a fixture
    force_status,
    new_run,
    periods_of,
    runs_world,
    session_for,
)
from tests.payroll.test_wage_inputs import CHECK, component, expect_error, set_setting, sql, tenant

WAGE_INPUTS = "/api/v1/payroll/wage-inputs"
LOCK_TIME = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def objects(db, run_id, employee_id=None):
    from app.models.pay_runs import PayrollRun, PayrollRunEmployee

    run = db.query(PayrollRun).filter(PayrollRun.id == run_id).one()
    if employee_id is None:
        return run, None
    row = db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == run_id,
                                              PayrollRunEmployee.employee_id == employee_id).one()
    return run, row


def built(company_id, run_id, employee_id, **kwargs):
    from app.payroll.services import snapshot

    db = session_for(company_id)
    try:
        run, row = objects(db, run_id, employee_id)
        result = snapshot.build(db, run, row, lock_time=LOCK_TIME, **kwargs)
        db.commit()
        return result
    finally:
        db.close()


def lock(company_id, run_id, employee_ids):
    from app.payroll.services import snapshot

    db = session_for(company_id)
    try:
        run, _ = objects(db, run_id)
        rows = [objects(db, run_id, e)[1] for e in employee_ids]
        result = snapshot.lock_inputs(db, run, rows, lock_time=LOCK_TIME)
        db.commit()
        return result
    finally:
        db.close()


@pytest.fixture
def snap_world(runs_world):
    """runs_world with attendance off (no unmarked-day review exceptions) and a regular run for the period."""
    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    w["run"] = new_run(w["acme"], w["group"], w["period"]["id"])
    w["start"], w["end"] = date.fromisoformat(w["period"]["start"]), date.fromisoformat(w["period"]["end"])
    return w


def emma(w):
    return w["emp"]["Emma"]["id"]


def ivan(w):
    return w["emp"]["Ivan"]["id"]


# ---------------------------------------------------------------------------
# Schema, engine and determinism
# ---------------------------------------------------------------------------
def test_snapshot_of_a_seeded_employee_passes_the_strict_schema_and_the_engine(snap_world):
    from app.payroll import engine
    from app.payroll.engine import snapshot as snap
    from app.payroll.services import snapshot

    w = snap_world
    db = session_for(w["cid"])
    try:
        run, row = objects(db, w["run"]["id"], emma(w))
        content = snapshot.build_snapshot(db, run, row, lock_time=LOCK_TIME)
    finally:
        db.close()
    parsed = snap.Snapshot.from_dict(content)          # strict: unknown keys, floats, naive datetimes refused
    assert parsed.to_dict() == content                 # stored in normalized form
    assert content["run"] == {"run_id": w["run"]["id"], "run_employee_id": row.id, "run_type": "REGULAR",
                              "environment": "test"}
    assert content["currency"] == "INR" and content["employee"]["country"] == "IN"
    period = content["period"]
    assert (period["start"], period["end"]) == (w["period"]["start"], w["period"]["end"])
    assert period["periods_per_year"] == 12 and period["number_in_tax_year"] >= 1
    # F-04: every date of the period, each employed date in a segment of this run.
    assert len(content["days"]) == (w["end"] - w["start"]).days + 1
    keys = {s["key"] for s in content["segments"]}
    assert all(d["segment"] in keys for d in content["days"] if d["employed"])
    assert [s["compensation"]["structure_type"] for s in content["segments"]] == ["CTC_BASED"]
    assert content["settings"]["payroll.attendance.enabled"] is False
    assert "payroll.overtime.enabled" in content["switch_ranges"]
    assert "ATTENDANCE_PAY_POLICY" in content["policies"] and content["policies"]["ATTENDANCE_PAY_POLICY"]["policy_hash"]
    assert {c["code"] for c in content["components"]} >= {"BASIC", "HRA", "FIXED_ALLOWANCE", "BONUS"}
    result = engine.calculate(content)
    assert result.content_hash == parsed.content_hash
    assert not [i for i in result.exceptions if i.code in ("LEDGER_INPUT_ERROR", "NO_SALARY", "CURRENCY_MISMATCH",
                                                           "CONFIG_ERROR")], result.exceptions
    assert result.amount("BASIC") > 0 and result.totals.gross > 0


def test_determinism_building_twice_gives_the_same_content_hash_and_sources(snap_world):
    w = snap_world
    first = built(w["cid"], w["run"]["id"], emma(w))
    second = built(w["cid"], w["run"]["id"], emma(w))
    assert first.content_hash == second.content_hash and first.content == second.content
    assert first.source_hashes == second.source_hashes
    sources = {k.split(":", 1)[0] for k in first.source_hashes}
    assert {"employee", "assignment", "compensation_assignment", "structure_version", "component_version",
            "settings", "policy_version", "day_ledger"} <= sources
    assert all(len(v) == 64 for v in first.source_hashes.values())
    other = built(w["cid"], w["run"]["id"], ivan(w))
    assert other.content_hash != first.content_hash


# ---------------------------------------------------------------------------
# Segments (EC-CMP-16), transfers (EC-ORG-06), settings ranges, the run's population only
# ---------------------------------------------------------------------------
def test_ec_cmp_16_mid_period_revision_gives_two_segments_that_add_up(snap_world):
    from app.payroll import engine

    w = snap_world
    middle = w["start"] + timedelta(days=15)
    assign_and_approve(w["acme"], w["approver"], ivan(w), w["version"], "1500000", effective_from=middle)
    content = built(w["cid"], w["run"]["id"], ivan(w)).content
    segments = content["segments"]
    assert [(s["start"], s["end"]) for s in segments] == [
        (w["start"].isoformat(), (middle - timedelta(days=1)).isoformat()), (middle.isoformat(), w["end"].isoformat())]
    assert segments[0]["compensation"]["id"] != segments[1]["compensation"]["id"]
    assert [Decimal(s["compensation"]["pay_basis_amount"]) for s in segments] == [Decimal("1200000"),
                                                                                    Decimal("1500000")]
    by_segment = {s["key"]: 0 for s in segments}
    for day in content["days"]:
        by_segment[day["segment"]] += 1
    assert sum(by_segment.values()) == len(content["days"])
    result = engine.calculate(content)
    basic = [line for line in result.lines if line.component == "BASIC"]
    assert len(basic) == 2 and all(line.amount > 0 for line in basic)


def test_ec_org_06_days_paid_by_another_legal_entity_are_not_employed_by_this_run(snap_world):
    w = snap_world
    middle = w["start"] + timedelta(days=10)
    dubai = sql("SELECT id FROM branches WHERE company_id = :c AND code = 'DXB'", c=w["cid"])[0][0]
    r = w["acme"].post(f"/api/v1/employees/{emma(w)}/assignments",
                       json={"branch_id": dubai, "effective_from": middle.isoformat(), "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    sql("UPDATE employee_assignments SET pay_group_id = :g WHERE employee_id = :e AND legal_entity_id = :le",
        g=w["ae_group"], e=emma(w), le=w["ae"])
    content = built(w["cid"], w["run"]["id"], emma(w)).content
    employed = [d["date"] for d in content["days"] if d["employed"]]
    assert employed[-1] == (middle - timedelta(days=1)).isoformat()      # entity A pays up to the transfer date
    assert all(not d["employed"] and d["segment"] == "" for d in content["days"] if d["date"] >= middle.isoformat())
    assert content["segments"][-1]["end"] == (middle - timedelta(days=1)).isoformat()
    assert content["employee"]["country"] == "IN"


def test_day_grain_switch_ranges_follow_a_mid_period_change(snap_world):
    w = snap_world
    middle = w["start"] + timedelta(days=12)
    sql("DELETE FROM setting_values WHERE company_id = :c AND key = 'payroll.overtime.enabled' "
        "AND scope_type = 'TENANT'", c=w["cid"])
    for start, end, value in (("2000-01-01", middle - timedelta(days=1), "false"), (middle, None, "true")):
        sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, "
            "effective_to, status, row_version, source, created_at, updated_at) VALUES (:c, 'payroll.overtime.enabled', "
            "'TENANT', NULL, CAST(:v AS jsonb), false, :f, :t, 'ACTIVE', 1, 'ADMIN', now(), now())",
            c=w["cid"], v=value, f=start, t=end)
    content = built(w["cid"], w["run"]["id"], emma(w)).content
    assert content["switch_ranges"]["payroll.overtime.enabled"] == [[middle.isoformat(), w["end"].isoformat()]]
    assert content["settings"]["payroll.overtime.enabled"] is True         # the PERIOD value on the facts date


def test_the_builder_refuses_rows_of_other_runs_and_excluded_employees(snap_world):
    from app.core.errors import AppError
    from app.payroll.services import snapshot

    w = snap_world
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    db = session_for(w["cid"])
    try:
        run, nora = objects(db, w["run"]["id"], w["emp"]["Nora"]["id"])
        with pytest.raises(AppError) as info:
            snapshot.build_snapshot(db, run, nora, lock_time=LOCK_TIME)      # excluded (NO_SALARY)
        assert info.value.code == "RUN_EMPLOYEE_EXCLUDED"
        _, emma_row = objects(db, w["run"]["id"], emma(w))
        emma_row.run_id = w["run"]["id"] + 100000                            # a row that is not this run's
        with pytest.raises(AppError) as info:
            snapshot.build_snapshot(db, run, emma_row, lock_time=LOCK_TIME)
        assert info.value.status_code == 404
        db.rollback()
    finally:
        db.close()
    beta_db = session_for(beta["cid"])
    try:
        from app.models.pay_runs import PayrollRunEmployee

        assert beta_db.query(PayrollRunEmployee).filter(PayrollRunEmployee.run_id == w["run"]["id"]).count() == 0
        assert beta_run["id"] != w["run"]["id"]
    finally:
        beta_db.close()


# ---------------------------------------------------------------------------
# Inputs: wage inputs, YTD per entity and tax year, previous period and pack state
# ---------------------------------------------------------------------------
def test_approved_wage_inputs_are_frozen_and_reserved_at_lock(runs_world):
    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    component(w["cid"], "SPOT_BONUS")                                    # an INPUT component in force since 2020
    start, end = w["period"]["start"], w["period"]["end"]
    r = w["acme"].post(WAGE_INPUTS, json={"employee_id": emma(w), "component_code": "SPOT_BONUS", "period_start": start,
                                          "period_end": end, "amount": {"amount": "2500.00", "currency": "INR"}})
    assert r.status_code == 201, r.text
    input_id = r.json()["items"][0]["id"]
    run = new_run(w["acme"], w["group"], w["period"]["id"])
    force_status(run["id"], "READY")
    result = lock(w["cid"], run["id"], [emma(w)])
    [stored] = result.snapshots.values()
    assert [(i["id"], i["component"], i["amount"]) for i in stored.content["wage_inputs"]] == [(input_id, "SPOT_BONUS",
                                                                                               "2500.00")]
    assert f"wage_input:{input_id}" in stored.source_hashes
    row_id = next(iter(result.snapshots))
    assert sql("SELECT reserved_by_run_employee_id FROM wage_inputs WHERE id = :i", i=input_id)[0][0] == row_id
    again = lock(w["cid"], run["id"], [emma(w)])                            # a retried lock keeps the reservation
    assert next(iter(again.snapshots.values())).content["wage_inputs"][0]["id"] == input_id
    from app.payroll import engine

    assert engine.calculate(stored.content).amount("SPOT_BONUS") == Decimal("2500.00")


def test_ec_cor_06_off_cycle_bonus_snapshot_keeps_only_the_selected_components(runs_world):
    from app.payroll import engine

    w = runs_world
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    set_setting(w["cid"], "payroll.wage_inputs.require_approval_above", {"amount": "99999.00", "currency": "INR"})
    component(w["cid"], "SPOT_BONUS")
    component(w["cid"], "SPOT_COMMISSION")
    start, end = w["period"]["start"], w["period"]["end"]
    ids = {}
    for code, amount in (("SPOT_BONUS", "2500.00"), ("SPOT_COMMISSION", "800.00")):
        r = w["acme"].post(WAGE_INPUTS, json={"employee_id": emma(w), "component_code": code, "period_start": start,
                                              "period_end": end, "amount": {"amount": amount, "currency": "INR"}})
        assert r.status_code == 201, r.text
        ids[code] = r.json()["items"][0]["id"]
    r = w["acme"].post("/api/v1/payroll/off-cycle-runs", json={
        "kind": "BONUS", "reason": "Diwali bonus", "pay_group_id": w["group"], "pay_period_id": w["period"]["id"],
        "employee_ids": [emma(w)], "component_codes": ["SPOT_BONUS"]})
    assert r.status_code == 201, r.text
    bonus_run = r.json()["id"]
    force_status(bonus_run, "READY")
    result = lock(w["cid"], bonus_run, [emma(w)])
    [stored] = result.snapshots.values()
    content = stored.content
    assert content["run"]["run_type"] == "OFF_CYCLE"
    assert [i["component"] for i in content["wage_inputs"]] == ["SPOT_BONUS"]    # the commission stays for payroll
    assert all(s["compensation"]["lines"] == [] for s in content["segments"] if s["compensation"])
    assert content["overtime_entries"] == [] and content["reimbursements"] == []
    assert not (content.get("tasks") or {}).get("records")
    assert f"run_selection:{bonus_run}" in stored.source_hashes
    reserved = dict(sql("SELECT id, reserved_by_run_employee_id FROM wage_inputs WHERE id = ANY(:i)",
                        i=list(ids.values())))
    assert reserved[ids["SPOT_BONUS"]] == stored.run_employee_id and reserved[ids["SPOT_COMMISSION"]] is None
    produced = {line.component for line in engine.calculate(content).lines if line.kind == "EARNING"}
    assert produced == {"SPOT_BONUS"}                                            # no BASIC / HRA in a bonus run
    # A regular run of the same period still freezes the salary and the unselected input.
    regular = new_run(w["acme"], w["group"], w["period"]["id"])
    full = built(w["cid"], regular["id"], emma(w)).content
    assert {i["component"] for i in full["wage_inputs"]} == {"SPOT_COMMISSION"}   # the bonus is reserved elsewhere
    assert full["segments"][0]["compensation"]["lines"]


def test_f_17_ec_sta_08_ytd_is_per_legal_entity_tax_year_and_currency(snap_world):
    w = snap_world
    tax_year = sql("SELECT tax_year FROM pay_periods WHERE id = :p", p=w["period"]["id"])[0][0]
    for entity, year, bucket, amount, currency in ((w["le01"], tax_year, "GROSS", "100000.0000", "INR"),
                                                   (w["le01"], tax_year, "IN_TDS", "5000.0000", "INR"),
                                                   (w["ae"], tax_year, "GROSS", "77777.0000", "INR"),
                                                   (w["le01"], "2001-02", "GROSS", "1.0000", "INR"),
                                                   (w["le01"], tax_year, "OTHER", "9.0000", "AED")):
        sql("INSERT INTO ytd_balances (company_id, employee_id, legal_entity_id, tax_year, bucket, amount, currency, "
            "row_version, created_at, updated_at) VALUES (:c, :e, :le, :y, :b, :a, :cur, 1, now(), now())",
            c=w["cid"], e=emma(w), le=entity, y=year, b=bucket, a=amount, cur=currency)
    done = built(w["cid"], w["run"]["id"], emma(w))
    assert done.content["ytd"] == {"GROSS": "100000.0000", "IN_TDS": "5000.0000"}
    assert len([k for k in done.source_hashes if k.startswith("ytd_balance:")]) == 2


def test_f_15_previous_finalized_period_feeds_variance_and_the_pack_state(snap_world):
    import json

    w = snap_world
    old = w["run"]                                     # this period, finalized below; the new run pays the next one
    following = next(p for p in periods_of(w["acme"], w["group"]) if p["start"] > w["period"]["end"])
    row_id = sql("SELECT id FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e", r=old["id"],
                 e=emma(w))[0][0]
    state = {"IN.esi": {"contribution_period": "2026-10-01..2027-03-31", "covered": True}}
    sql("UPDATE payroll_run_employees SET status = 'CALCULATED', gross = 100000, net = 90000, "
        "stage_outputs = CAST(:s AS jsonb) WHERE id = :i",
        s=json.dumps({"statutory": {"pack_state": state}}), i=row_id)
    for code, kind, amount in (("BASIC", "EARNING", "50000.0000"), ("HRA", "EARNING", "20000.0000")):
        sql("INSERT INTO payroll_lines (company_id, run_id, run_employee_id, component_code, kind, stage, amount, "
            "currency, tags, visibility, sources, trace, sort_order, statutory, fixture, created_at, updated_at) "
            "VALUES (:c, :r, :re, :code, :k, 4, :a, 'INR', '[]', 'EMPLOYEE_VISIBLE', '[]', '{}', 0, false, false, "
            "now(), now())", c=w["cid"], r=old["id"], re=row_id, code=code, k=kind, a=amount)
    force_status(old["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    run = new_run(w["acme"], w["group"], following["id"])
    content = built(w["cid"], run["id"], emma(w)).content
    assert content["previous"]["period_ref"] == f"{w['period']['start']}..{w['period']['end']}"
    assert content["previous"]["gross"] == "100000.0000" and content["previous"]["net"] == "90000.0000"
    assert content["previous"]["components"] == {"BASIC": "50000.0000", "HRA": "20000.0000"}
    assert content["previous"]["history"]["BASIC"] == ["50000.0000"]
    assert content["pack_state"] == state                                  # EC-STA-17 eligibility carried
    other = built(w["cid"], run["id"], ivan(w)).content                     # Ivan was not paid by the old run
    assert other["previous"].get("period_ref") is None and other["pack_state"] == {}


def test_determinations_come_from_stage_outputs_then_flags_never_invented(snap_world):
    from app.payroll.services import snapshot

    w = snap_world
    db = session_for(w["cid"])
    try:
        run, row = objects(db, w["run"]["id"], emma(w))
        builder = snapshot._Builder(db, run, row, lock_time=LOCK_TIME)
        stored = SimpleNamespace(id=1, stage_outputs={"statutory": {"pack_state": {"K": {"a": 1}}}}, flags=[],
                                 calc_hash=None)
        assert builder.determinations(stored, None) == {"K": {"a": 1}}
        flagged = SimpleNamespace(id=2, stage_outputs=None, calc_hash=None, flags=[
            {"code": "PACK_STATE_DETERMINED", "details": {"pack_state": {"F": {"b": 2}}}}])
        assert builder.determinations(flagged, None) == {"F": {"b": 2}}
        nothing = SimpleNamespace(id=3, stage_outputs=None, flags=["OTHER"], calc_hash=None)
        assert builder.determinations(nothing, {"pack": None}) == {}
        broken = SimpleNamespace(id=4, stage_outputs=None, flags=[], calc_hash="0" * 64)
        assert builder.determinations(broken, {"pack": {"code": "IN"}, "nonsense": True}) == {}
        assert [i.code for i in builder.issues] == ["PACK_STATE_UNAVAILABLE"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# FX (EC-CMP-07): converted by the assignment's rule or the run's snapshot, never 1:1
# ---------------------------------------------------------------------------
def test_contract_currency_is_converted_by_a_fixed_rate_or_reported_missing(snap_world):
    from app.payroll import engine

    w = snap_world
    comp_id = sql("SELECT id FROM compensation_assignments WHERE employee_id = :e", e=ivan(w))[0][0]
    sql("ALTER TABLE compensation_assignments DISABLE TRIGGER USER")
    try:
        sql("UPDATE compensation_assignments SET currency = 'USD', pay_basis_amount = 15000, "
            "fx_rule = CAST(:r AS jsonb) WHERE id = :i", r='{"source": "FIXED_RATE", "rate": "80"}', i=comp_id)
        fixed = built(w["cid"], w["run"]["id"], ivan(w))
        compensation = fixed.content["segments"][0]["compensation"]
        assert compensation["currency"] == "INR" and Decimal(compensation["pay_basis_amount"]) == Decimal("1200000")
        assert fixed.content["fx"]["conversions"][0]["source"] == "FIXED_RATE"
        assert not [i for i in fixed.issues if i.code == "FX_MISSING"]
        assert engine.calculate(fixed.content).amount("BASIC") > 0
        sql("UPDATE compensation_assignments SET fx_rule = CAST(:r AS jsonb) WHERE id = :i",
            r='{"source": "PERIOD_SNAPSHOT"}', i=comp_id)
        missing = built(w["cid"], w["run"]["id"], ivan(w))
    finally:
        sql("ALTER TABLE compensation_assignments ENABLE TRIGGER USER")
    assert [i.code for i in missing.issues if i.severity == "BLOCKING"] == ["FX_MISSING"]
    assert missing.content["segments"][0]["compensation"]["currency"] == "USD"    # never converted 1:1
    assert [i.code for i in engine.calculate(missing.content).blocking] == ["CURRENCY_MISMATCH"]


# ---------------------------------------------------------------------------
# lock_inputs: versions, immutability, refusals, concurrency, isolation
# ---------------------------------------------------------------------------
def test_lock_inputs_stores_immutable_versions_and_schedules_change_detection(snap_world):
    from app.payroll.services import change_detection

    w = snap_world
    force_status(w["run"]["id"], "READY")
    first = lock(w["cid"], w["run"]["id"], [emma(w), ivan(w)])
    assert len(first.snapshots) == 2 and not first.failed
    rows = sql("SELECT e.employee_id, s.version, s.content_hash, e.snapshot_id = s.id FROM payroll_input_snapshots s "
               "JOIN payroll_run_employees e ON e.id = s.run_employee_id WHERE e.run_id = :r ORDER BY 1",
               r=w["run"]["id"])
    assert [(r[1], r[3]) for r in rows] == [(1, True), (1, True)]
    second = lock(w["cid"], w["run"]["id"], [emma(w)])
    stored = next(iter(second.snapshots.values()))
    assert stored.version == 2 and stored.content_hash == first.snapshots[stored.run_employee_id].content_hash
    with tenant(w["cid"]) as conn:
        expect_error(conn, CHECK, "UPDATE payroll_input_snapshots SET content_hash = :h WHERE id = :i",
                     h="0" * 64, i=stored.id)
    jobs = sql("SELECT status, run_at FROM jobs WHERE company_id = :c AND type = :t", c=w["cid"],
               t=change_detection.MATCH_JOB)
    assert jobs and jobs[0][0] == "QUEUED"


def test_lock_inputs_refuses_draft_finalized_excluded_and_foreign_rows(snap_world):
    from app.core.errors import AppError
    from app.payroll.services import snapshot

    w = snap_world
    beta = w["beta"]
    beta_run = new_run(beta["client"], beta["group"], beta["period"]["id"])
    with pytest.raises(AppError) as info:
        lock(w["cid"], w["run"]["id"], [emma(w)])
    assert info.value.code == "RUN_NOT_READY"                               # DRAFT
    force_status(w["run"]["id"], "READY")
    with pytest.raises(AppError) as info:
        lock(w["cid"], w["run"]["id"], [w["emp"]["Nora"]["id"]])
    assert info.value.code == "RUN_EMPLOYEE_EXCLUDED"
    beta_row = sql("SELECT id FROM payroll_run_employees WHERE run_id = :r", r=beta_run["id"])[0][0]
    db = session_for(w["cid"])
    try:
        run, _ = objects(db, w["run"]["id"])
        with pytest.raises(AppError) as info:
            snapshot.lock_inputs(db, run, [beta_row])                          # another tenant's row id
        assert info.value.status_code == 404
        db.rollback()
    finally:
        db.close()
    beta_db = session_for(beta["cid"])                                         # Beta cannot lock Acme's run
    try:
        from app.models.pay_runs import PayrollRun

        foreign = SimpleNamespace(id=w["run"]["id"], company_id=w["cid"])
        with pytest.raises(AppError) as info:
            snapshot.lock_inputs(beta_db, foreign, [1])
        assert info.value.status_code == 404
        assert beta_db.query(PayrollRun).filter(PayrollRun.id == w["run"]["id"]).count() == 0
        beta_db.rollback()
    finally:
        beta_db.close()
    force_status(w["run"]["id"], "FINALIZED", finalized_at=datetime.now(timezone.utc))
    with pytest.raises(AppError) as info:
        lock(w["cid"], w["run"]["id"], [emma(w)])
    assert info.value.code == "RUN_FINALIZED" and info.value.headers.get("X-Payroll-Hint") == "CREATE_CORRECTION"


def test_two_concurrent_locks_serialize_on_the_run_and_never_duplicate_a_version(snap_world):
    w = snap_world
    force_status(w["run"]["id"], "READY")
    errors, results = [], []

    def worker():
        try:
            results.append(lock(w["cid"], w["run"]["id"], [emma(w), ivan(w)]))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    versions = sql("SELECT s.run_employee_id, array_agg(s.version ORDER BY s.version), count(DISTINCT s.content_hash) "
                   "FROM payroll_input_snapshots s JOIN payroll_run_employees e ON e.id = s.run_employee_id "
                   "WHERE e.run_id = :r GROUP BY 1", r=w["run"]["id"])
    assert sorted(v[1] for v in versions) == [[1, 2], [1, 2]] and all(v[2] == 1 for v in versions)


def test_ec_sec_snapshots_are_isolated_across_three_tenants(snap_world):
    from app.models.pay_runs import PayrollInputSnapshot

    w = snap_world
    force_status(w["run"]["id"], "READY")
    lock(w["cid"], w["run"]["id"], [emma(w)])
    for other in (w["beta"], w["cobalt"]):
        db = session_for(other["cid"])
        try:
            assert db.query(PayrollInputSnapshot).count() == 0
        finally:
            db.close()
        with tenant(other["cid"]) as conn:
            from sqlalchemy import text

            assert conn.execute(text("SELECT count(*) FROM payroll_input_snapshots")).scalar() == 0
    with tenant(w["cid"]) as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM payroll_input_snapshots")).scalar() == 1


# ---------------------------------------------------------------------------
# Hooks: F-14 environment for pack constraints, F-36 pay figures, policy_in_use (EC-CFG-16)
# ---------------------------------------------------------------------------
def test_f_14_pack_constraints_use_the_environment(monkeypatch):
    from app.payroll import packs
    from app.payroll.services import settings as settings_engine
    from app.payroll.services import snapshot

    assert settings_engine.pack_constraints is snapshot.pack_constraints
    found = settings_engine.pack_constraints("IN", date(2026, 10, 31))
    assert [(c.target, c.required_value) for c in found] == [("payroll.statutory.IN.tds.enabled", True)]
    assert all(c.required_value is False for c in packs.setting_constraints("IN", date(2026, 10, 31)))
    calls = []
    monkeypatch.setattr(packs, "setting_constraints", lambda country, on: calls.append((country, on)) or [])
    assert settings_engine.pack_constraints("ZZ", date(2026, 10, 31)) == ()   # older two-argument hooks still work
    assert calls == [("ZZ", date(2026, 10, 31))] and settings_engine.pack_constraints(None, date(2026, 1, 1)) == ()


def test_f_36_pay_figures_provider_reads_compensation_and_the_last_finalized_net(snap_world):
    from app.payroll.services import loans, snapshot

    w = snap_world
    db = session_for(w["cid"])
    try:
        figures = snapshot.pay_figures(db, w["cid"], emma(w), w["end"], "INR")
        assert figures.monthly_gross is not None and figures.monthly_gross > 0 and figures.monthly_net is None
        assert snapshot.pay_figures(db, w["cid"], emma(w), w["end"], "USD").monthly_gross is None
        assert snapshot.pay_figures(db, w["cid"], w["emp"]["Nora"]["id"], w["end"], "INR").monthly_gross is None
        assert loans._figures_provider is snapshot.pay_figures
    finally:
        db.close()


def test_ec_cfg_16_policy_in_use_answers_from_the_stored_snapshots(snap_world):
    from app.models.payroll_config import Policy
    from app.payroll.services import policies, snapshot

    w = snap_world
    assert policies.policy_in_use is snapshot.policy_in_use
    force_status(w["run"]["id"], "READY")
    result = lock(w["cid"], w["run"]["id"], [emma(w)])
    content = next(iter(result.snapshots.values())).content
    version_id = content["policies"]["ATTENDANCE_PAY_POLICY"]["id"]
    db = session_for(w["cid"])
    try:
        used = sql("SELECT policy_id FROM policy_versions WHERE id = :v", v=version_id)[0][0]
        assert policies.policy_in_use(db, w["cid"], used) is True
        unused = [p.id for p in db.query(Policy).filter(Policy.company_id == w["cid"]).all()
                  if not sql("SELECT 1 FROM policy_versions WHERE policy_id = :p AND id = ANY(:ids)", p=p.id,
                             ids=[v["id"] for v in content["policies"].values()])]
        assert unused and policies.policy_in_use(db, w["cid"], unused[0]) is False
        assert policies.policy_in_use(db, w["beta"]["cid"], used) is False     # another tenant never sees it
    finally:
        db.close()
    r = w["acme"].post(f"/api/v1/payroll/policies/{used}/archive")
    assert r.status_code == 200 and r.json()["used_by_runs"] is True, r.text
