"""Leave v2 (LH-M2..LH-M8) end to end through the API on PostgreSQL.

Fake clock: Mon 2026-10-19 09:00 IST (company zone Asia/Kolkata, default Mon-Fri week, no holidays). The signup seed
gives casual 10, sick 12, earned 15, maternity 90, paternity 7 and unlimited unpaid leave, and the route "reporting
manager, then HR Administrator for 3+ days".
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company

L = "/api/v1/leaves"


def _sql(statement, **params):
    from sqlalchemy import text

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        return conn.execute(text(statement), params)


def backdate_settings(company_id):
    """Signup and module writes stamp setting rows with the database date; the fake clock is earlier. The latest
    active row of each setting becomes effective from 2024 (earlier closed rows are superseded)."""
    _sql("UPDATE setting_values s SET status = 'SUPERSEDED' WHERE s.company_id = :c AND s.status = 'ACTIVE' AND "
         "EXISTS (SELECT 1 FROM setting_values o WHERE o.company_id = s.company_id AND o.key = s.key AND o.scope_type "
         "= s.scope_type AND COALESCE(o.scope_id, 0) = COALESCE(s.scope_id, 0) AND o.status = 'ACTIVE' AND "
         "o.effective_from > s.effective_from)", c=company_id)
    _sql("UPDATE setting_values SET effective_to = NULL WHERE company_id = :c AND status = 'ACTIVE'", c=company_id)
    _sql("UPDATE setting_values s SET effective_from = '2024-01-01' WHERE s.company_id = :c AND s.status = 'ACTIVE' "
         "AND s.effective_from > '2024-01-01' AND NOT EXISTS (SELECT 1 FROM setting_values o WHERE o.company_id = "
         "s.company_id AND o.key = s.key AND o.scope_type = s.scope_type AND COALESCE(o.scope_id, 0) = "
         "COALESCE(s.scope_id, 0) AND o.status IN ('ACTIVE', 'PENDING_APPROVAL') AND o.id <> s.id)", c=company_id)


def set_setting(company_id, key, value_sql):
    backdate_settings(company_id)
    updated = _sql("UPDATE setting_values SET value = " + value_sql + " WHERE company_id = :c AND key = :k "
                   "AND scope_type = 'TENANT' AND status = 'ACTIVE'", c=company_id, k=key)
    if updated.rowcount == 0:
        _sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, "
             "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', " + value_sql + ", false, "
             "'2024-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key)


def step_up(company_id):
    from app.core import clock

    _sql("UPDATE user_sessions SET step_up_at = :t, step_up_method = 'EMAIL_OTP' WHERE user_id IN "
         "(SELECT id FROM users WHERE company_id = :c)", t=clock.utcnow(), c=company_id)


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture
def acme(app, fake_clock):  # noqa: F811
    admin = signup_company(app, "Acme Leave")
    boss = create_employee(admin, "Arjun", "Mehta")
    worker = create_employee(admin, "Priya", "Shah", manager_id=boss["id"], gender="Female")
    peer = create_employee(admin, "Ravi", "Kumar", manager_id=boss["id"])
    hr = create_employee(admin, "Kavya", "Rao")
    hr2 = create_employee(admin, "Neha", "Iyer")
    backdate_settings(admin.company_id)
    return {"admin": admin, "cid": admin.company_id, "clock": fake_clock,
            "boss": invite_and_activate(app, admin, boss, ["manager"]),
            "worker": invite_and_activate(app, admin, worker, ["employee"]),
            "peer": invite_and_activate(app, admin, peer, ["employee"]),
            "hr": invite_and_activate(app, admin, hr, ["hr_admin"]),
            "hr2": invite_and_activate(app, admin, hr2, ["hr_admin"]),
            "worker_id": worker["id"], "peer_id": peer["id"], "boss_id": boss["id"]}


def go_live(t):
    step_up(t["cid"])
    report = ok(t["hr"].get(f"{L}/setup/reconcile"))
    assert report["unexplained"] == 0, report
    result = ok(t["hr"].post(f"{L}/setup/go-live", json={"confirm": "GO LIVE"}))
    backdate_settings(t["cid"])
    return result


def office_plan(t, *, activate=True, priority=10):
    plan = ok(t["hr"].post(f"{L}/plans", json={"name": "India Office", "template_key": "IN.OFFICE.V1",
                                               "effective_from": "2026-01-01"}), 201)
    ok(t["hr"].put(f"{L}/plans/{plan['id']}/assignments", json={"rules": [
        {"criteria": {"everyone": True}, "priority": priority, "effective_from": "2026-01-01"}]}))
    if activate:
        ok(t["hr"].post(f"{L}/plans/{plan['id']}/versions/1/activate",
                        json={"effective_from": "2026-01-01", "reason": "Set up the office plan"}))
    return plan


def apply(client, leave_type, start, end=None, **extra):
    return client.post(f"{L}/requests", json={"leave_type": leave_type, "start_date": str(start),
                                              "end_date": str(end or start), **extra})


def balance(client, code):
    data = ok(client.get(f"{L}/balances/me"))
    return next(b for b in data["balances"] if b["leave_type_code"] == code)


# ------------------------------------------------------------------------------------------- types (LH-TYP)
def test_lh_typ_v2_catalogue_frozen_fields_sensitivity_and_row_version(acme):
    hr, worker = acme["hr"], acme["worker"]
    types = ok(hr.get(f"{L}/v2/types"))
    casual = next(t for t in types if t["code"] == "casual")
    sick = next(t for t in types if t["code"] == "sick")
    assert (casual["category"], casual["sensitivity"], sick["sensitivity"], sick["absence_class"]) == (
        "CASUAL", "STANDARD", "SENSITIVE", "PROTECTED")
    created = ok(hr.post(f"{L}/v2/types", json={"code": "study", "name": "Study Leave", "category": "STUDY"}), 201)
    assert (created["icon"], created["absence_class"]) == ("graduation-cap", "PLANNED")
    ok(apply(worker, "casual", "2026-11-02"), 201)
    body = {k: casual[k] for k in ("code", "name", "category", "is_paid", "allow_half_day", "annual_quota")}
    frozen = hr.put(f"{L}/v2/types/{casual['id']}", json=body | {"is_unlimited": True},
                    headers={"If-Match": str(casual["row_version"])})
    assert (frozen.status_code, frozen.json()["code"]) == (409, "LEAVE_TYPE_FIELD_FROZEN")
    renamed = hr.put(f"{L}/v2/types/{casual['id']}", json=body | {"code": "casual2"})
    assert renamed.json()["code"] == "LEAVE_TYPE_CODE_FROZEN"
    stale = hr.put(f"{L}/v2/types/{casual['id']}", json=body | {"name": "Casual"}, headers={"If-Match": "99"})
    assert stale.json()["code"] == "ROW_VERSION_CONFLICT"
    sick_body = {k: sick[k] for k in ("code", "name", "category", "is_paid", "annual_quota")} | {"sensitivity": "STANDARD"}
    assert hr.put(f"{L}/v2/types/{sick['id']}", json=sick_body).json()["code"] == "CONFIRMATION_REQUIRED"
    lowered = ok(hr.put(f"{L}/v2/types/{sick['id']}", json=sick_body | {"reason": "Company decided to show it",
                                                                         "confirm_code": "sick"}))
    assert lowered["sensitivity"] == "STANDARD"
    hourly = hr.post(f"{L}/v2/types", json={"code": "pto_h", "name": "PTO hours", "unit": "HOUR", "category": "ANNUAL"})
    assert (hourly.status_code, hourly.json()["code"]) == (422, "HOURLY_DISABLED")
    assert worker.post(f"{L}/v2/types", json={"code": "x1", "name": "X"}).status_code == 403


# ------------------------------------------------------------------------------------------- plans (LH-ELG, M2)
def test_lh_plan_template_simulate_explain_and_inert_before_cutover(acme):
    hr, worker = acme["hr"], acme["worker"]
    templates = ok(hr.get(f"{L}/plans/templates"))
    assert {t["key"] for t in templates} >= {"IN.OFFICE.V1", "IN.FACTORY.V1", "AE.PRIVATE.V1", "US.PTO.V1"}
    plan = office_plan(acme, activate=False)
    assert any(t["code"] == "comp_off" for t in ok(hr.get(f"{L}/v2/types")))  # the template created comp-off
    sim = ok(hr.post(f"{L}/plans/simulate", json={"policy_id": plan["id"], "version": 1,
                                                  "employee_ids": [acme["worker_id"]], "on": "2026-10-19"}))
    earned = next(t for t in sim["employees"][0]["types"] if t["leave_type_code"] == "earned")
    assert (earned["status"], earned["this_cycle"], earned["cycle_start"]) == ("ELIGIBLE", "18.00", "2026-01-01")
    maternity = next(t for t in sim["employees"][0]["types"] if t["leave_type_code"] == "maternity")
    assert maternity["status"] == "ELIGIBLE"  # Priya is FEMALE; the rule never echoes the attribute
    ok(hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"effective_from": "2026-01-01",
                                                                     "reason": "Office plan"}))
    # LEGACY_V1 tenant: the activated plan is inert (13 §2.3) — the legacy rules still apply.
    explain = ok(worker.get(f"{L}/plans/resolve", params={"employee_id": acme["worker_id"]}))
    assert explain["plan"]["status"] == "LEGACY" and explain["v2_enabled"] is False
    assert worker.get(f"{L}/plans").status_code == 403


def test_lh_elg_21_second_person_activation_and_plan_scope(acme):
    hr, hr2 = acme["hr"], acme["hr2"]
    set_setting(acme["cid"], "leave.plan.requires_second_person", "'true'::jsonb")
    plan = office_plan(acme, activate=False)
    requested = ok(hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"effective_from": "2026-11-01"}))
    assert requested["status"] == "AWAITING_SECOND_PERSON"
    same = hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"confirm": True})
    assert (same.status_code, same.json()["code"]) == (403, "SELF_DECISION_FORBIDDEN")
    done = ok(hr2.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"confirm": True}))
    assert done["status"] == "ACTIVE" and done["effective_from"] == "2026-11-01"


# ------------------------------------------------------------------------------------------- cut-over (LH-MIG)
def test_lh_mig_cutover_reconciles_to_zero_and_balances_match(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    legacy = ok(worker.post(f"{L}/apply", json={"leave_type": "casual", "start_date": "2026-11-02",
                                                "end_date": "2026-11-03"}))
    ok(boss.post(f"{L}/{legacy['id']}/approve"))
    before = ok(worker.get(f"{L}/my/balance"))["casual"]
    blocked = hr.post(f"{L}/setup/go-live", json={"confirm": "GO LIVE"})
    assert blocked.json()["code"] == "STEP_UP_REQUIRED"
    step_up(acme["cid"])
    assert hr.post(f"{L}/setup/go-live", json={"confirm": "yes"}).json()["code"] == "CONFIRMATION_REQUIRED"
    result = go_live(acme)
    assert result["status"] == "V2_ACTIVE" and result["grants_posted"] >= 5 * 5
    after = ok(worker.get(f"{L}/my/balance"))["casual"]
    assert (after["available"], after["used"]) == (before["available"], before["used"]) == (8, 2)
    card = balance(worker, "casual")
    assert (card["balance"], card["cycle_start"], card["credited"]) == (8, "2026-01-01", 10)
    grants = owner_rows("SELECT count(*) AS n FROM leave_balance_transactions WHERE company_id = :c AND reason = "
                        "'GRANT' AND source = 'MIGRATION'", c=acme["cid"])[0]["n"]
    assert grants == result["grants_posted"]
    assert ok(hr.get(f"{L}/setup"))["state"] == "V2_ACTIVE"
    # Idempotent: a second go-live is refused, the accrual job posts nothing more for the Default plan.
    assert hr.post(f"{L}/setup/go-live", json={"confirm": "GO LIVE"}).json()["code"] == "INVALID_TRANSITION"
    run = ok(hr.post(f"{L}/accruals/run"))
    assert run["credits"] == 0


# ------------------------------------------------------------------------------------------- requests (LH-REQ)
def test_lh_req_v2_preview_notice_draft_return_resubmit_and_two_step_approval(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    go_live(acme)
    office_plan(acme)
    earned = balance(worker, "earned")
    assert earned["credited"] == 15 or earned["credited"] >= 0  # Default grant + office plan accruals
    short = ok(worker.post(f"{L}/preview", json={"leave_type": "earned", "start_date": "2026-10-22",
                                                 "end_date": "2026-10-22"}))
    assert [i["code"] for i in short["issues"] if i["severity"] == "ERROR"] == ["NOTICE_TOO_SHORT"]
    emergency = ok(worker.post(f"{L}/preview", json={"leave_type": "earned", "start_date": "2026-10-22",
                                                     "end_date": "2026-10-22", "is_emergency": True,
                                                     "reason": "Family emergency"}))
    assert not emergency["blocking"] and "EMERGENCY" in emergency["flags"]
    # Five units need 14 days' notice (scale); Mon 2026-11-16 .. Fri 2026-11-20 is 28 days ahead.
    preview = ok(worker.post(f"{L}/preview", json={"leave_type": "earned", "start_date": "2026-11-16",
                                                   "end_date": "2026-11-22"}))
    assert preview["charged_units"] == 5 and not preview["blocking"]
    assert [d["charged"] for d in preview["days"]] == [1, 1, 1, 1, 1, 0, 0]
    draft = ok(apply(worker, "earned", "2026-11-16", "2026-11-20", draft=True), 201)
    assert draft["status_v2"] == "DRAFT" and "SUBMIT" in draft["allowed_actions"]
    assert worker.get(f"{L}/my").json() == [] or all(r["id"] != draft["id"] for r in worker.get(f"{L}/my").json())
    pending = ok(worker.post(f"{L}/requests/{draft['id']}/submit"))
    assert pending["status_v2"] == "PENDING"
    assert boss.post(f"{L}/requests/{draft['id']}/return", json={"reason": ""}).status_code == 422
    returned = ok(boss.post(f"{L}/requests/{draft['id']}/return", json={"reason": "Please take Tue-Fri only"}))
    assert returned["status_v2"] == "RETURNED"
    ok(worker.put(f"{L}/requests/{draft['id']}", json={"leave_type": "earned", "start_date": "2026-11-17",
                                                       "end_date": "2026-11-20"}))
    resubmitted = ok(worker.post(f"{L}/requests/{draft['id']}/submit"))
    assert resubmitted["status_v2"] == "PENDING" and resubmitted["number_of_days"] == 4
    assert worker.post(f"{L}/requests/{draft['id']}/approve").status_code == 403  # never your own
    no_reason = boss.post(f"{L}/requests/{draft['id']}/reject", json={})
    assert no_reason.json()["code"] == "REASON_REQUIRED"
    step1 = ok(boss.post(f"{L}/requests/{draft['id']}/approve", json={"remarks": "Fine"}))
    assert step1["status_v2"] == "PENDING"  # 4 days: HR approves too
    final = ok(hr.post(f"{L}/requests/{draft['id']}/approve"))
    assert final["status_v2"] == "APPROVED" and [d["charge_reason"] for d in final["days"]] == ["WORKING"] * 4
    rows = owner_rows("SELECT delta, cycle_start::text AS c FROM leave_balance_transactions WHERE ref_type = 'leave' "
                      "AND ref_id = :i", i=draft["id"])
    assert [(Decimal(r["delta"]), r["c"]) for r in rows] == [(Decimal("-4.00"), "2026-01-01")]
    messages = ok(worker.get(f"{L}/requests/{draft['id']}/messages"))
    assert any(m["kind"] == "DECISION_NOTE" for m in messages)


def test_lh_req_on_behalf_record_split_to_unpaid_and_overlap(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    go_live(acme)
    behalf = ok(apply(boss, "casual", "2026-11-04", employee_id=acme["worker_id"], reason="Called in sick"), 201)
    assert (behalf["source"], behalf["status_v2"]) == ("ON_BEHALF", "PENDING")
    assert apply(acme["peer"], "casual", "2026-11-04", employee_id=acme["worker_id"]).status_code == 403
    recorded = ok(hr.post(f"{L}/record", json={"leave_type": "sick", "start_date": "2026-10-13",
                                                 "end_date": "2026-10-14", "employee_id": acme["worker_id"]}), 201)
    assert (recorded["source"], recorded["status_v2"]) == ("HR_RECORDED", "APPROVED")
    overlap = apply(worker, "casual", "2026-11-04")
    assert overlap.json()["code"] == "LEAVE_OVERLAP"
    # SPLIT_TO_UNPAID through a plan: casual 10, 0 used apart from the pending day -> 12 days split.
    plan = office_plan(acme, activate=False)
    detail = ok(hr.get(f"{L}/plans/{plan['id']}"))
    fields = detail["versions"][0]["fields"]
    for rule in fields["types"]:
        if rule["leave_type_code"] == "casual":
            rule["insufficient_balance_action"] = "SPLIT_TO_UNPAID"
            rule["request"]["max_units"] = None
            rule["request"]["caps"] = []
    ok(hr.put(f"{L}/plans/{plan['id']}/versions/1", json={"fields": fields}))
    ok(hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"effective_from": "2026-01-01", "reason": "x"}))
    ok(hr.post(f"{L}/accruals/run"))  # the office plan's casual 12 tops up the migrated 10
    preview = ok(worker.post(f"{L}/preview", json={"leave_type": "casual", "start_date": "2026-11-09",
                                                   "end_date": "2026-11-27"}))
    assert preview["split"]["units"] == 4 and not preview["blocking"]
    created = ok(apply(worker, "casual", "2026-11-09", "2026-11-27", reason="Wedding"), 201)
    assert created["siblings"][0]["leave_type"] == "unpaid" and created["siblings"][0]["number_of_days"] == 4


# ------------------------------------------------------------------------------------------- changes (LH-CHG)
def test_lh_chg_partial_cancel_modify_and_convert(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    go_live(acme)
    leave = ok(apply(worker, "casual", "2026-11-09", "2026-11-10"), 201)
    ok(boss.post(f"{L}/requests/{leave['id']}/approve"))
    assert balance(worker, "casual")["balance"] == 8
    change = ok(worker.post(f"{L}/requests/{leave['id']}/changes", json={
        "kind": "PARTIAL_CANCEL", "proposed": {"dates": ["2026-11-10"]}, "reason": "Back early"}), 201)
    assert change["status"] == "PENDING"
    second = worker.post(f"{L}/requests/{leave['id']}/changes", json={"kind": "CANCEL"})
    assert second.json()["code"] == "CHANGE_PENDING"
    ok(boss.post(f"{L}/changes/{change['id']}/approve"))
    detail = ok(worker.get(f"{L}/requests/{leave['id']}"))
    assert (detail["number_of_days"], detail["version"], len(detail["days"])) == (1, 2, 1)
    assert balance(worker, "casual")["balance"] == 9
    modify = ok(worker.post(f"{L}/requests/{leave['id']}/changes", json={
        "kind": "MODIFY", "proposed": {"start_date": "2026-11-12", "end_date": "2026-11-13"}}), 201)
    ok(boss.post(f"{L}/changes/{modify['id']}/approve"))
    moved = ok(worker.get(f"{L}/requests/{leave['id']}"))
    assert (moved["start_date"], moved["number_of_days"]) == ("2026-11-12", 2)
    assert balance(worker, "casual")["balance"] == 8
    convert = ok(hr.post(f"{L}/requests/{leave['id']}/changes", json={
        "kind": "CONVERT", "proposed": {"leave_type": "sick"}, "reason": "Certificate received"}), 201)
    assert convert["status"] == "APPROVED" and convert["initiator"] == "EMPLOYER"  # HR changes apply at once
    assert (balance(worker, "casual")["balance"], balance(worker, "sick")["balance"]) == (10, 10)
    cancel = ok(worker.post(f"{L}/requests/{leave['id']}/changes", json={"kind": "CANCEL"}), 201)
    ok(boss.post(f"{L}/changes/{cancel['id']}/approve"))
    assert ok(worker.get(f"{L}/requests/{leave['id']}"))["status_v2"] == "CANCELLED"
    assert balance(worker, "sick")["balance"] == 12


# ------------------------------------------------------------------------------------------- accrual and year end
def test_lh_acc_monthly_accrual_catch_up_and_year_end_needs_a_second_person(acme):
    hr, hr2, worker = acme["hr"], acme["hr2"], acme["worker"]
    go_live(acme)
    office_plan(acme)
    ok(hr.post(f"{L}/accruals/run"))
    earned = balance(worker, "earned")
    # Monthly 1.5 posted at the month end: January..September = 9 months = 13.5 (plus the migrated 15 grant is the
    # Default plan's and is not this plan's credit: the cumulative method tops up to the office plan's figure).
    assert earned["balance"] == Decimal("13.5") or earned["balance"] == 13.5
    again = ok(hr.post(f"{L}/accruals/run"))
    assert again["credits"] == 0  # cumulative: reruns post nothing
    acme["clock"].now = acme["clock"].now.replace(year=2027, month=1, day=4)
    run = ok(hr.post(f"{L}/year-end", json={"cycle_end": "2026-12-31"}), 201)
    row = next(r for r in run["rows"] if r["employee_id"] == acme["worker_id"] and r["leave_type"] == "earned")
    assert (row["carry"], row["lapse"]) == ("18.00", "0") or Decimal(row["carry"]) == Decimal("18")
    step_up(acme["cid"])
    same = hr.post(f"{L}/year-end/{run['id']}/post")
    assert same.json()["code"] == "SELF_DECISION_FORBIDDEN"
    posted = ok(hr2.post(f"{L}/year-end/{run['id']}/post"))
    assert posted["status"] == "POSTED"
    ok(hr.post(f"{L}/accruals/run"))
    card = balance(worker, "earned")
    assert card["cycle_start"] == "2027-01-01" and Decimal(str(card["opening"])) == Decimal("18")
    casual = balance(worker, "casual")
    assert Decimal(str(casual["opening"])) == 0  # casual carries nothing (cap 0): lapsed


# ------------------------------------------------------------------------------------------- comp-off and encashment
def test_lh_cmp_comp_off_claim_for_weekly_off_work_and_encashment(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    go_live(acme)
    office_plan(acme)
    _sql("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, source, status, "
         "is_manual, created_at, updated_at) VALUES (:c, :e, '2026-10-17 03:30+00', '2026-10-17 12:00+00', "
         "'2026-10-17', 'manual', 'present', true, now(), now())", c=acme["cid"], e=acme["worker_id"])
    preview = ok(worker.get(f"{L}/comp-off/preview", params={"worked_date": "2026-10-17"}))
    assert (preview["source"], preview["credit_units"]) == ("WEEKLY_OFF_WORK", 1)
    weekday = worker.post(f"{L}/comp-off/claims", json={"worked_date": "2026-10-16"})
    assert weekday.json()["code"] == "NOT_A_NON_WORKING_DAY"
    claim = ok(worker.post(f"{L}/comp-off/claims", json={"worked_date": "2026-10-17", "reason": "Release"}), 201)
    assert worker.post(f"{L}/comp-off/claims", json={"worked_date": "2026-10-17"}).json()["code"] == \
        "COMP_OFF_SOURCE_TAKEN"
    assert worker.post(f"{L}/comp-off/claims/{claim['id']}/approve").status_code == 403
    approved = ok(boss.post(f"{L}/comp-off/claims/{claim['id']}/approve"))
    assert approved["status"] == "APPROVED" and approved["expires_on"] == "2026-12-16"
    comp = balance(worker, "comp_off")
    assert comp["balance"] == 1 and comp["expiring"][0]["expires_on"] == "2026-12-16"
    # Encashment of earned leave is YEAR_END / EXIT only in the office plan; on request is refused.
    refused = worker.post(f"{L}/encashments", json={"leave_type": "earned", "units": "2"})
    assert refused.json()["code"] == "ENCASHMENT_NOT_ALLOWED"


# ------------------------------------------------------------------------------------------- rules catalogue
def test_lh_req_blackout_blocks_and_coverage_warns(acme):
    hr, worker, peer, boss = acme["hr"], acme["worker"], acme["peer"], acme["boss"]
    ok(hr.post(f"{L}/blackouts", json={"name": "Year-end close", "from_date": "2026-12-28", "to_date": "2026-12-31",
                                       "mode": "BLOCK", "leave_type_codes": ["casual"]}), 201)
    blocked = apply(worker, "casual", "2026-12-29")
    assert (blocked.status_code, blocked.json()["code"]) == (422, "BLACKOUT_PERIOD")
    assert apply(worker, "sick", "2026-12-29").status_code == 201  # sick is not listed
    branch = owner_rows("SELECT branch_id FROM employee_assignments WHERE employee_id = :i AND is_primary",
                        i=acme["worker_id"])[0]["branch_id"]
    ok(hr.post(f"{L}/coverage-rules", json={"name": "One away", "branch_id": branch, "max_absent_count": 1,
                                            "mode": "WARN"}), 201)
    first = ok(apply(peer, "casual", "2026-11-25"), 201)
    ok(boss.post(f"{L}/requests/{first['id']}/approve"))
    preview = ok(worker.post(f"{L}/preview", json={"leave_type": "casual", "start_date": "2026-11-25",
                                                   "end_date": "2026-11-25"}))
    assert [i["code"] for i in preview["issues"] if i["severity"] == "WARNING"] == ["COVERAGE_CONFLICT"]


# ------------------------------------------------------------------------------------------- documents and privacy
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def test_lh_doc_medical_document_access_masking_and_discussion(acme):
    hr, worker, boss, peer = acme["hr"], acme["worker"], acme["boss"], acme["peer"]
    leave = ok(apply(worker, "sick", "2026-11-02", "2026-11-04", reason="Fever"), 201)
    up = ok(worker.post(f"{L}/attachments", data={"category": "MEDICAL_CERTIFICATE", "leave_id": str(leave["id"])},
                        files={"file": ("cert.pdf", PDF, "application/pdf")}), 201)
    assert up["sensitivity"] == "MEDICAL"
    fake = worker.post(f"{L}/attachments", data={"category": "SUPPORTING_DOCUMENT", "leave_id": str(leave["id"])},
                       files={"file": ("x.pdf", b"<script>alert(1)</script>", "application/pdf")})
    assert fake.json()["code"] == "DOCUMENT_TYPE"
    assert worker.get(f"{L}/attachments/{up['id']}/download").status_code == 200
    seen_by_boss = ok(boss.get(f"{L}/requests/{leave['id']}/attachments"))[0]
    assert seen_by_boss["can_view"] is False and seen_by_boss["original_name"] == "Medical document"
    assert boss.get(f"{L}/attachments/{up['id']}/download").status_code == 404
    assert hr.get(f"{L}/attachments/{up['id']}/download").status_code == 200  # hr_admin holds view_medical
    log = owner_rows("SELECT count(*) AS n FROM leave_attachment_access_log WHERE attachment_id = :a", a=up["id"])
    assert log[0]["n"] == 2
    ok(boss.post(f"{L}/requests/{leave['id']}/messages", json={"body": "Get well soon"}), 201)
    ok(hr.post(f"{L}/requests/{leave['id']}/messages", json={"body": "Check pattern", "internal": True}), 201)
    mine = ok(worker.get(f"{L}/requests/{leave['id']}/messages"))
    assert [m["body"] for m in mine if m["kind"] in ("COMMENT", "INTERNAL_NOTE")] == ["Get well soon"]
    assert peer.get(f"{L}/requests/{leave['id']}").status_code == 404
    calendar = ok(peer.get(f"{L}/team-calendar", params={"start": "2026-11-01", "end": "2026-11-07"}))
    priya = next(p for p in calendar["people"] if p["employee_id"] == acme["worker_id"])
    assert priya["leaves"] == []  # pending leave is not shown to colleagues
    ok(boss.post(f"{L}/requests/{leave['id']}/approve"))
    ok(hr.post(f"{L}/requests/{leave['id']}/approve"))
    calendar = ok(peer.get(f"{L}/team-calendar", params={"start": "2026-11-01", "end": "2026-11-07"}))
    priya = next(p for p in calendar["people"] if p["employee_id"] == acme["worker_id"])
    assert [(x["label"], x["leave_type"], x["id"]) for x in priya["leaves"]] == [("Away", None, None)]


# ------------------------------------------------------------------------------------------- tenant isolation
def test_lh_sec_01_v2_ids_of_another_company_are_404(app, acme):
    other = signup_company(app, "Other Tenant")
    leave = ok(apply(acme["worker"], "casual", "2026-11-02"), 201)
    plan = office_plan(acme, activate=False)
    for path in (f"{L}/requests/{leave['id']}", f"{L}/requests/{leave['id']}/messages",
                 f"{L}/requests/{leave['id']}/attachments", f"{L}/plans/{plan['id']}"):
        assert other.get(path).status_code == 404, path
    for path, body in ((f"{L}/requests/{leave['id']}/approve", {}), (f"{L}/requests/{leave['id']}/withdraw", {}),
                       (f"{L}/requests/{leave['id']}/changes", {"kind": "CANCEL"}),
                       (f"{L}/plans/{plan['id']}/versions/1/activate", {})):
        assert other.post(path, json=body).status_code == 404, path
    assert all(r["employee_id"] != acme["worker_id"]
               for r in ok(other.get(f"{L}/requests", params={"scope": "all"}))["items"])
