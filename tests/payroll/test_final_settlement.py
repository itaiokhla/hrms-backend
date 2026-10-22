"""Final settlement: cases per employment period, termination types, the checklist, due dates, consent for
asset deductions, the FINAL_SETTLEMENT run and the F&F event, rehire, permissions and three-tenant isolation
(3.5 §15).

EC-COR-07, EC-COR-08, EC-COR-09, EC-COR-12, EC-COR-13, EC-COR-14, EC-SEC-01, EC-SEC-05.
"""

from datetime import date, timedelta

import pytest

from tests.payroll.test_retro import (  # noqa: F401  (fixtures)
    calculate,
    finalize,
    retro_world,
    runs_world,
    working_day,
)
from tests.payroll.test_runs_lifecycle import PAY, RUNS
from tests.payroll.test_wage_inputs import audit_actions, sql

CASES = f"{PAY}/final-settlements"
DEADLINES = f"{PAY}/final-settlement-deadlines"


def terminate(employee_id, day):
    sql("UPDATE employees SET termination_date = :d WHERE id = :e", d=day, e=employee_id)


def open_case(client, employee_id, last_day, expect=201, termination_type="RESIGNATION", **extra):
    body = {"employee_id": employee_id, "termination_type": termination_type, "last_working_day": str(last_day),
            **extra}
    r = client.post(CASES, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def case(client, case_id, expect=200):
    r = client.get(f"{CASES}/{case_id}")
    assert r.status_code == expect, r.text
    return r.json()


def item(data, item_type, source_key=None):
    return next(i for i in data["items"] if i["item_type"] == item_type
                and (source_key is None or i["source_key"] == source_key))


def settle_items(client, data):
    """Decide every open item: confirm calculated ones, waive the others with a reason."""
    for entry in data["items"]:
        if entry["status"] not in ("PENDING", "BLOCKED"):
            continue
        if entry["item_type"] in ("SALARY_TO_LAST_DAY", "TAX", "PENDING_ARREARS", "PENDING_CLAIMS", "PENDING_INPUTS",
                                  "LOAN_RECOVERY") and entry["status"] == "PENDING":
            r = client.post(f"{CASES}/{data['id']}/items/{entry['id']}/confirm", json={})
        else:
            r = client.post(f"{CASES}/{data['id']}/items/{entry['id']}/waive",
                            json={"note": "Not due under the contract"})
        assert r.status_code == 200, r.text
    return case(client, data["id"])


@pytest.fixture
def fs_world(runs_world):
    w = runs_world
    w["ivan"] = w["emp"]["Ivan"]["id"]
    w["emma"] = w["emp"]["Emma"]["id"]
    w["last_day"] = working_day(w["period"], offset=2)
    terminate(w["ivan"], w["last_day"])
    return w


# ---------------------------------------------------------------------------
# Due dates (EC-COR-08)
# ---------------------------------------------------------------------------
def test_ec_cor_08_due_date_from_the_company_deadline_is_tracked_not_verified_and_overdue(fs_world):
    w = fs_world
    created = open_case(w["acme"], w["ivan"], w["last_day"])
    data = case(w["acme"], created["id"])
    assert data["due_basis"] == "NOT_CONFIGURED" and data["due_date"] is None
    assert "configure a final-settlement deadline" in data["due_note"]
    assert "not verified" in data["due_note"]  # the pack's own deadline text is shown, never applied
    r = w["acme"].put(DEADLINES, json={"days": 2, "day_basis": "WORKING_DAYS", "note": "Company policy"})
    assert r.status_code == 200, r.text
    refreshed = w["acme"].post(f"{CASES}/{created['id']}/checklist/refresh", json={})
    assert refreshed.status_code == 200, refreshed.text
    data = case(w["acme"], created["id"])
    expected, added, weekend = w["last_day"], 0, _weekend(w)
    while added < 2:  # two working days after the last day, by hand
        expected += timedelta(days=1)
        if expected.weekday() not in weekend:
            added += 1
    assert data["due_basis"] == "COMPANY_DEADLINE" and data["due_date"] == expected.isoformat()
    assert "not verified" in data["due_note"].lower()
    assert data["overdue"] is True  # the last day is in the past
    listed = w["acme"].get(CASES, params={"overdue": True}).json()
    assert listed["overdue"] == 1 and [c["id"] for c in listed["items"]] == [created["id"]]
    assert "FINAL_SETTLEMENT_DEADLINE_SAVED" in [a for (a,) in sql(
        "SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = 'final_settlement_deadline'",
        c=w["cid"])]
    r = w["acme"].put(DEADLINES, json={"days": 400, "day_basis": "CALENDAR_DAYS"})
    assert r.status_code == 422
    r = w["processor"].put(DEADLINES, json={"days": 3, "day_basis": "CALENDAR_DAYS"})
    assert r.status_code == 403


def _weekend(w):
    from app.payroll import countries

    country = sql("SELECT country_code FROM legal_entities WHERE id = :e", e=w["le01"])[0][0]
    return countries.default_weekend(country)


# ---------------------------------------------------------------------------
# Checklist, notice shortfall, the settlement run and the F&F event (EC-COR-13)
# ---------------------------------------------------------------------------
def test_ec_cor_13_checklist_notice_recovery_needs_the_contract_and_the_settlement_run_settles(fs_world):
    w = fs_world
    created = open_case(w["acme"], w["ivan"], w["last_day"], notice_given_on=str(w["last_day"] - timedelta(days=10)))
    data = case(w["acme"], created["id"])
    assert data["status"] == "OPEN" and data["employment_start"] == "2024-01-15"
    types = {i["item_type"] for i in data["items"]}
    assert {"SALARY_TO_LAST_DAY", "NOTICE_PAY", "GRATUITY", "STATUTORY_BONUS", "PENDING_ARREARS", "PENDING_CLAIMS",
            "PENDING_INPUTS", "LOAN_RECOVERY", "TAX"} <= types
    notice = item(data, "NOTICE_PAY")
    assert notice["details"]["days_served"] == 11
    gratuity = item(data, "GRATUITY")
    assert gratuity["amount"] is None  # never invented: no pack rule here
    url = f"{CASES}/{created['id']}/items/{notice['id']}/confirm"
    r = w["acme"].post(url, json={"amount": {"amount": "-5000.00", "currency": "INR"}})
    assert r.status_code == 400 and r.json()["code"] == "CONTRACT_REFERENCE_REQUIRED"
    r = w["acme"].post(url, json={"amount": {"amount": "-5000.00", "currency": "AED"}, "contract_ref": "Clause 9"})
    assert r.status_code == 400 and r.json()["code"] == "CURRENCY_MISMATCH"
    r = w["acme"].post(url, json={"amount": {"amount": -5000, "currency": "INR"}, "contract_ref": "Clause 9"})
    assert r.status_code == 422  # a JSON number in a money field
    r = w["acme"].post(url, json={"amount": {"amount": "-5000.00", "currency": "INR"},
                                  "contract_ref": "Offer letter clause 9", "note": "Shortfall of 20 days"})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    r = w["acme"].post(f"{CASES}/{created['id']}/items/{gratuity['id']}/confirm", json={})
    assert r.status_code in (400, 409)  # an amount from a qualified source is needed, or a waiver
    r = w["acme"].post(f"{CASES}/{created['id']}/run", json={})
    assert r.status_code == 409 and r.json()["code"] == "CASE_NOT_READY"
    data = settle_items(w["acme"], case(w["acme"], created["id"]))
    assert data["status"] == "READY" and data["open_items"] == 0
    assert data["totals"]["recovered"] == "-5000.00"
    r = w["acme"].post(f"{CASES}/{created['id']}/run", json={})
    assert r.status_code == 201 and r.json()["run_type"] == "FINAL_SETTLEMENT", r.text
    run_id = r.json()["id"]
    data = case(w["acme"], created["id"])
    assert data["status"] == "IN_RUN" and data["run"]["id"] == run_id
    wage_input = sql("SELECT component_code, amount, source, external_ref FROM wage_inputs WHERE company_id = :c "
                     "AND employee_id = :e", c=w["cid"], e=w["ivan"])
    assert [(c, str(a), s) for c, a, s, _ in wage_input] == [("FS_NOTICE_RECOVERY", "5000.0000", "SYSTEM")]
    # The checklist is frozen while the run exists.
    r = w["acme"].post(f"{CASES}/{created['id']}/checklist/refresh", json={})
    assert r.status_code == 409
    # The run follows the normal lifecycle; once finalized the case is settled and the F&F statement is asked for.
    calculate(w["acme"], run_id)
    finalize(w, run_id)
    data = case(w["acme"], created["id"])
    assert data["status"] == "SETTLED" and data["settled_at"]
    topics = [t for (t,) in sql("SELECT topic FROM outbox_events WHERE company_id = :c ORDER BY id", c=w["cid"])]
    assert "payroll.final_settlement.finalized" in topics
    assert "FINAL_SETTLEMENT_SETTLED" in audit_actions(w["cid"], "final_settlement_case", created["id"])
    # One settlement per employment period.
    open_case(w["acme"], w["ivan"], w["last_day"], expect=409)


def test_case_rules_last_day_payee_and_permissions(fs_world):
    w = fs_world
    open_case(w["acme"], w["ivan"], w["last_day"] + timedelta(days=1), expect=409)  # LAST_DAY_MISMATCH
    open_case(w["acme"], w["ivan"], w["last_day"], expect=422, termination_type="FIRED")
    open_case(w["acme"], w["ivan"], w["last_day"], expect=400,
              payee={"type": "NOMINEE", "name": "A", "relationship": "Spouse", "evidence_ref": "Form F"})
    open_case(w["processor"], w["ivan"], w["last_day"])  # the processor prepares settlements
    open_case(w["approver"], w["ivan"], w["last_day"], expect=403)
    open_case(w["cleo"], w["ivan"], w["last_day"], expect=404)  # LE01 is outside the clerk's scope
    r = w["acme"].post(CASES, json={"employee_id": w["ivan"], "termination_type": "RESIGNATION",
                                    "last_working_day": str(w["last_day"]), "status": "SETTLED"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Death and absconding (EC-COR-07, EC-COR-12)
# ---------------------------------------------------------------------------
def test_ec_cor_07_death_pays_a_named_nominee_and_ec_cor_12_absconding_is_held(fs_world):
    w = fs_world
    open_case(w["acme"], w["ivan"], w["last_day"], expect=400, termination_type="DEATH")  # PAYEE_REQUIRED
    created = open_case(w["acme"], w["ivan"], w["last_day"], termination_type="DEATH",
                        payee={"type": "NOMINEE", "name": "Irene Test", "relationship": "Spouse",
                               "evidence_ref": "Nomination form 2"})
    data = case(w["acme"], created["id"])
    assert data["payee"] == {"type": "NOMINEE", "name": "Irene Test", "relationship": "Spouse",
                             "evidence_ref": "Nomination form 2"}
    assert "account" not in str(data["payee"]).lower()  # bank details go through the bank account workflow
    r = w["acme"].post(f"{CASES}/{created['id']}/cancel", json={"reason": "Recorded against the wrong employee"})
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    held = open_case(w["acme"], w["ivan"], w["last_day"], termination_type="ABSCONDING")
    assert held["held"] is True
    data = case(w["acme"], held["id"])
    assert "EC-COR-12" in data["hold_reason"]
    r = w["acme"].post(f"{CASES}/{held['id']}/release-hold", json={"note": "Returned"})
    assert r.status_code == 403  # the case's creator does not release its hold
    r = w["approver"].post(f"{CASES}/{held['id']}/release-hold", json={"note": "Employee returned the laptop"})
    assert r.status_code == 200 and r.json()["held"] is False, r.text
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError) as info:  # an absconding case cannot lose its hold reason silently (CHECK)
        sql("UPDATE final_settlement_cases SET held = true, hold_reason = NULL WHERE id = :i", i=held["id"])
    assert info.value.orig.pgcode == "23514"
    with pytest.raises(DBAPIError):  # a death case always names its payee (CHECK)
        sql("UPDATE final_settlement_cases SET payee = NULL WHERE id = :i", i=created["id"])


def test_ec_cor_12_the_settlement_run_of_an_absconding_employee_holds_the_payment(fs_world):
    w = fs_world
    held = open_case(w["acme"], w["ivan"], w["last_day"], termination_type="ABSCONDING")
    data = settle_items(w["acme"], case(w["acme"], held["id"]))
    assert data["status"] == "READY" and data["held"] is True
    r = w["acme"].post(f"{CASES}/{held['id']}/run", json={})
    assert r.status_code == 201, r.text
    [(hold,)] = sql("SELECT hold FROM payroll_run_employees WHERE run_id = :r AND employee_id = :e",
                    r=r.json()["id"], e=w["ivan"])
    assert hold["type"] == "PAYMENT" and hold["reason_code"] == "ABSCONDING"
    assert case(w["acme"], held["id"])["status"] == "IN_RUN"


# ---------------------------------------------------------------------------
# Gratuity from a draft pack (EC-COR-07: entitlements by type, never invented)
# ---------------------------------------------------------------------------
def test_ec_cor_07_end_of_service_from_a_draft_pack_is_blocked_as_not_verified(fs_world):
    w = fs_world
    gus = w["gus"]["id"]
    terminate(gus, w["last_day"])
    created = open_case(w["acme"], gus, w["last_day"], termination_type="REDUNDANCY")
    data = case(w["acme"], created["id"])
    gratuity = item(data, "GRATUITY")
    assert gratuity["status"] == "BLOCKED" and gratuity["amount"] is None
    assert gratuity["issues"][0]["code"] == "GRATUITY_NOT_VERIFIED"
    assert "not verified" in gratuity["issues"][0]["message"].lower()
    url = f"{CASES}/{created['id']}/items/{gratuity['id']}/confirm"
    r = w["acme"].post(url, json={"amount": {"amount": "12000.00", "currency": "AED"}})
    assert r.status_code == 400 and r.json()["code"] == "NOTE_REQUIRED"
    r = w["acme"].post(url, json={"amount": {"amount": "12000.00", "currency": "AED"},
                                  "note": "Computed by the labour consultant, letter 14 Oct"})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    assert data["currency"] == "AED"


# ---------------------------------------------------------------------------
# Assets (EC-COR-14)
# ---------------------------------------------------------------------------
def test_ec_cor_14_asset_deduction_needs_consent_another_approver_and_the_asset_value_cap(fs_world):
    w = fs_world
    r = w["acme"].post("/api/v1/assets", json={"asset_tag": "LT-001", "name": "Laptop", "category": "IT",
                                                "purchase_cost": 45000})
    assert r.status_code in (200, 201), r.text
    asset = r.json()
    r = w["acme"].post(f"/api/v1/assets/{asset['id']}/assign", json={"employee_id": w["ivan"]})
    assert r.status_code in (200, 201), r.text
    created = open_case(w["processor"], w["ivan"], w["last_day"])
    data = case(w["acme"], created["id"])
    entry = item(data, "ASSET_RECOVERY")
    assert entry["requires_consent"] is True and entry["details"]["valuation_cap"] in ("45000", "45000.00")
    url = f"{CASES}/{created['id']}/items/{entry['id']}/confirm"
    body = {"amount": {"amount": "-30000.00", "currency": "INR"}, "note": "Laptop not returned"}
    assert w["processor"].post(url, json=body).status_code == 403  # an approver decides
    r = w["approver"].post(url, json=body)
    assert r.status_code == 400 and r.json()["code"] == "CONSENT_REQUIRED", r.text
    r = w["approver"].post(url, json={**body, "amount": {"amount": "-50000.00", "currency": "INR"},
                                      "consent_ref": "Signed consent 2026-10-08"})
    assert r.status_code == 400 and r.json()["code"] == "ABOVE_ASSET_VALUE"
    r = w["approver"].post(url, json={**body, "consent_ref": "Signed consent 2026-10-08"})
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    stored = sql("SELECT consent_ref, consent_recorded_by_user_id FROM final_settlement_items WHERE id = :i",
                 i=entry["id"])[0]
    assert stored == ("Signed consent 2026-10-08", w["approver"].user_id)
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError):  # never a confirmed asset deduction without consent (CHECK)
        sql("UPDATE final_settlement_items SET consent_ref = NULL WHERE id = :i", i=entry["id"])


# ---------------------------------------------------------------------------
# Rehire (EC-COR-09)
# ---------------------------------------------------------------------------
def test_ec_cor_09_rehire_after_settlement_starts_a_new_employment_period(fs_world):
    w = fs_world
    first = open_case(w["acme"], w["ivan"], w["last_day"])
    open_case(w["acme"], w["ivan"], w["last_day"], expect=409)  # DUPLICATE_CASE for the same period
    rehired_on = w["last_day"] + timedelta(days=3)
    second_last = rehired_on + timedelta(days=4)
    placement = sql("SELECT legal_entity_id, branch_id, pay_group_id FROM employee_assignments WHERE employee_id = :e "
                    "AND is_primary ORDER BY effective_from DESC LIMIT 1", e=w["ivan"])[0]
    sql("UPDATE employee_assignments SET effective_to = :d WHERE employee_id = :e AND is_primary AND "
        "effective_to IS NULL", d=w["last_day"], e=w["ivan"])
    sql("INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, pay_group_id, "
        "is_primary, reason, effective_from, fte, created_at, updated_at) VALUES (:c, :e, :le, :b, :pg, true, "
        "'REHIRE', :f, 1, now(), now())", c=w["cid"], e=w["ivan"], le=placement[0], b=placement[1],
        pg=placement[2], f=rehired_on)
    terminate(w["ivan"], second_last)
    second = open_case(w["acme"], w["ivan"], second_last)
    assert case(w["acme"], second["id"])["employment_start"] == rehired_on.isoformat()
    old = case(w["acme"], first["id"])
    assert old["status"] == "OPEN" and old["last_working_day"] == w["last_day"].isoformat()  # untouched


# ---------------------------------------------------------------------------
# Isolation (EC-SEC-01)
# ---------------------------------------------------------------------------
def test_ec_sec_01_final_settlements_across_tenants(fs_world):
    w = fs_world
    created = open_case(w["acme"], w["ivan"], w["last_day"])
    data = case(w["acme"], created["id"])
    any_item = data["items"][0]
    w["acme"].put(DEADLINES, json={"days": 7, "day_basis": "CALENDAR_DAYS"})
    for tenant in (w["beta"], w["cobalt"]):
        other = tenant["client"]
        case(other, created["id"], expect=404)
        assert other.get(CASES).json()["total"] == 0
        assert other.get(DEADLINES).json()["items"] == []
        r = other.post(f"{CASES}/{created['id']}/items/{any_item['id']}/waive", json={"note": "x"})
        assert r.status_code == 404
        r = other.post(f"{CASES}/{created['id']}/run", json={})
        assert r.status_code == 404
        r = other.post(CASES, json={"employee_id": w["ivan"], "termination_type": "RESIGNATION",
                                    "last_working_day": str(w["last_day"])})
        assert r.status_code == 404
        r = other.put(DEADLINES, json={"legal_entity_id": w["le01"], "days": 1, "day_basis": "CALENDAR_DAYS"})
        assert r.status_code == 404
    case(w["cleo"], created["id"], expect=404)
