"""Regressions found while seeding the three-company payroll dataset (scripts/seed_payroll_three_companies.py).

1. The country packs' employer choices (``payroll.statutory.<CC>.*``) were never registered in the running
   API (``packs.register_settings`` was documented as "called at application start-up" but nobody called
   it): a legal entity could not switch on a scheme, so the IN fixture's mandatory income-tax withholding
   stayed OFF and every Alpha employee failed calculation with STATUTORY_SCHEME_OFF.
2. A wage input for a mid-period joiner's first period was refused with NOT_PLACED, because the placement
   was looked up on the period start (before the joining day) although the run pays the joiner for it.
3. A run paying a WITH_PAYROLL claim was refused at submission with RUN_STALE (see the test).
"""

from datetime import timedelta
from decimal import Decimal

from tests.helpers import create_employee
from tests.payroll.test_runs_lifecycle import RUNS, new_run, run_employees, runs_world  # noqa: F401  (fixture)
from tests.payroll.test_wage_inputs import (  # noqa: F401  (fixtures)
    BASE,
    body,
    inputs_world,
    next_month,
    place,
    set_setting,
    sql,
    world,
)


def test_startup_registers_pack_settings_so_an_entity_can_switch_on_a_scheme(world):
    from app.main import start_payroll_runtime
    from app.payroll.settings_registry import pack_setting_keys, unregister_pack_settings

    acme, entity = world["u"]["founder"], world["le01"]["id"]
    r = acme.patch(f"/api/v1/legal-entities/{entity}", json={"country_code": "IN", "base_currency": "INR",
                                                              "timezone": "Asia/Kolkata", "confirm_country": True})
    assert r.status_code == 200, r.text
    tds, pt = "payroll.statutory.IN.tds.enabled", "payroll.statutory.IN.pt.enabled"

    def values():
        r = acme.get("/api/v1/payroll/settings/values", params={"scope_type": "LEGAL_ENTITY", "scope_id": entity})
        assert r.status_code == 200, r.text
        return {i["key"]: i for i in r.json()["items"]}

    before = set(pack_setting_keys())
    if tds not in before:                                   # nothing registered them earlier in this process
        assert tds not in values() and pt not in values()
    start_payroll_runtime()                                 # the API's start-up hook (JOBS_MODE=inline: no worker)
    try:
        items = values()
        # The IN fixture (test environment) makes withholding mandatory: required by law, from the pack.
        assert items[tds]["value"] is True and items[tds]["level"] == "COUNTRY_PACK"
        assert items[tds]["badge"]["code"] == "REQUIRED_BY_LAW"
        # Professional tax is the employer's choice per legal entity: OFF by default, and now switchable.
        assert items[pt]["value"] is False and items[pt]["editable"] is True
        r = acme.put("/api/v1/payroll/settings/values", json={"changes": [{
            "key": pt, "scope_type": "LEGAL_ENTITY", "scope_id": entity, "value": True,
            "row_version": items[pt]["row_version"], "reason": "Professional tax (fixture pack, test environment)"}]})
        assert r.status_code == 200, r.text
        assert values()[pt]["value"] is True
    finally:
        for country in sorted({key.split(".")[2] for key in set(pack_setting_keys()) - before}):
            unregister_pack_settings(country)


def test_wage_input_for_a_mid_period_joiner_is_accepted_for_the_joining_period(inputs_world):
    w = inputs_world
    pia = w["u"]["pia"]
    start, end = w["period"]
    joined = start + timedelta(days=1)                       # first placed one day after the period start
    nina = create_employee(w["u"]["founder"], "Nina", "Joiner", hire_date=joined.isoformat())
    place(nina["id"], w["le01"]["id"], w["groups"]["IN"])
    w["emp"]["Nina"] = nina["id"]
    r = pia.post(BASE, json=body(w, employee="Nina", code="COMMISSION", amount="1340.00"))
    assert r.status_code == 201, r.text
    [item] = r.json()["items"]
    stored = sql("SELECT pay_group_id, employee_id, target_period_start FROM wage_inputs WHERE id = :i", i=item["id"])
    # The whole joining period, paid by the run of the joiner's pay group.
    assert stored == [(w["groups"]["IN"], nina["id"], start)]

    # Someone who joins only after the period still has no placement in it: refused as before.
    later_start, _ = next_month(end)
    late = create_employee(w["u"]["founder"], "Lars", "Later", hire_date=(later_start + timedelta(days=2)).isoformat())
    place(late["id"], w["le01"]["id"], w["groups"]["IN"])
    w["emp"]["Lars"] = late["id"]
    r = pia.post(BASE, json=body(w, employee="Lars", code="COMMISSION", amount="10.00"))
    assert r.status_code == 409 and r.json()["code"] == "NOT_PLACED", r.text


def test_a_run_paying_a_payroll_routed_claim_is_not_stale_at_submission(runs_world):
    """3. Locking a run's inputs schedules its WITH_PAYROLL claims (APPROVED -> SCHEDULED) right after the snapshot
    hashed them; the claim's change-detection hash included status and row_version, so the full pass at the
    submission gate (change_detection.gate_full_check) saw every scheduled claim as UPDATED and refused with
    409 RUN_STALE although nothing had changed (Beta and Gamma of the seed could not submit their runs)."""
    from tests.payroll.test_run_calculation import calculate as calculate_job
    from tests.payroll.test_run_calculation import no_pack_country, precheck_ready
    from tests.payroll.test_run_finalize import _claim, code_of, reject, run_state, submit
    from tests.payroll.test_components import ok

    w = runs_world
    acme = w["acme"]
    no_pack_country(w)
    set_setting(w["cid"], "payroll.attendance.enabled", False)
    emma = w["emp"]["Emma"]["id"]
    claim = _claim(w, emma, "300.00")            # approved, paid with payroll, dated in the period

    run = new_run(acme, w["group"], w["period"]["id"])
    precheck_ready(acme, run["id"])
    calculate_job(acme, run["id"])
    assert run_state(run["id"])["status"] == "REVIEW_REQUIRED"
    emma_row = run_employees(run["id"])[emma][2]
    assert sql("SELECT status, scheduled_ref FROM reimbursement_claims WHERE id = :c", c=claim)[0] == (
        "SCHEDULED", f"run_employee:{emma_row}")
    paid = sql("SELECT sum(amount) FROM payroll_lines WHERE run_employee_id = :r AND component_code = 'REIMBURSEMENT'",
               r=emma_row)[0][0]
    assert paid == Decimal("300.0000")
    for item in ok(acme.get(f"{RUNS}/{run['id']}/variance", params={"only_flagged": True}))["items"]:
        if item["pending"]:
            ok(acme.post(f"{RUNS}/{run['id']}/variance/{item['run_employee_id']}/acknowledge",
                         json={"comment": "Expected: first run"}))
    submit(acme, run["id"])                      # 200: nothing changed after the lock
    assert run_state(run["id"])["status"] == "APPROVAL_PENDING"
    assert sql("SELECT stale FROM payroll_runs WHERE id = :r", r=run["id"])[0][0] is False

    # A real change after the lock is still caught at the gate: the approver sends the run back, the claim's
    # approved amount is then corrected, and resubmission is refused as stale.
    reject(w["approver"], run["id"])
    ok(acme.post(f"{RUNS}/{run['id']}/rework", json={"note": "Claim amount corrected"}))
    sql("UPDATE reimbursement_claims SET approved_amount = 250, row_version = row_version + 1 WHERE id = :c", c=claim)
    r = submit(acme, run["id"], expect=409)
    assert code_of(r) == "RUN_STALE"
    stale, changed = sql("SELECT stale, stale_reasons FROM payroll_runs WHERE id = :r", r=run["id"])[0]
    assert stale is True
    assert [c["source"] for reason in changed for c in reason.get("changes", [])] == [f"claim:{claim}"]
