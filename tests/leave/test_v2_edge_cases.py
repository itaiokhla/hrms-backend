"""Leave v2 edge cases (LH edge-case catalogue, docs/leave-holiday/edge-cases.json) on PostgreSQL through the API.

Fake clock Mon 2026-10-19 09:00 IST (tests/leave/test_v2_core.py fixtures; default Mon-Fri week, no holidays). Most
tests go live and activate a variant of the India office template: earned 18/yr accrued monthly at the month end
(13.5 on 19 Oct), casual 12 front-loaded (max 3 per request and per month), sick 12, comp-off, unpaid.
"""

import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from tests.attendance_helpers import IST, fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import login
from tests.leave.test_v2_core import (  # noqa: F401  (acme is a fixture)
    L, _sql, acme, apply, balance, go_live, ok, office_plan, step_up,
)


def custom_plan(t, **edits):
    """The India office plan with ``edits[code](rule)`` applied to type rules, activated from 2026-01-01; accrued."""
    hr = t["hr"]
    plan = office_plan(t, activate=False)
    fields = ok(hr.get(f"{L}/plans/{plan['id']}"))["versions"][0]["fields"]
    for rule in fields["types"]:
        if rule["leave_type_code"] in edits:
            edits[rule["leave_type_code"]](rule)
    ok(hr.put(f"{L}/plans/{plan['id']}/versions/1", json={"fields": fields}))
    ok(hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"effective_from": "2026-01-01",
                                                                     "reason": "Edge-case plan"}))
    ok(hr.post(f"{L}/accruals/run"))
    return plan


def request_rules(rule, **values):
    rule.setdefault("request", {}).update(values)


def preview(client, leave_type, start, end=None, **extra):
    return ok(client.post(f"{L}/preview", json={"leave_type": leave_type, "start_date": start, "end_date": end or start,
                                                **extra}))


def errors(pv):
    return [i["code"] for i in pv["issues"] if i["severity"] == "ERROR"]


def approve_all(t, leave_id):
    for approver in (t["boss"], t["hr"]):
        if owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"] != "pending":
            break
        ok(approver.post(f"{L}/requests/{leave_id}/approve", json={"remarks": "ok"}))
    return owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"]


def at_ist(t, day, hhmm="10:00"):
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = (int(x) for x in hhmm.split(":"))
    t["clock"].now = IST.localize(datetime(y, m, d, h, mi)).astimezone(timezone.utc)


def tenant(company_id):
    from tests.holiday.test_rollover import tenant as committing_tenant

    return committing_tenant(company_id)


# ------------------------------------------------------------------------------------------- eligibility (LH-ELG)
def test_lh_elg_01_probation_accrue_only_block_and_min_tenure(acme):
    worker, peer = acme["worker"], acme["peer"]
    go_live(acme)
    custom_plan(acme, earned=lambda r: r.update(during_probation="ACCRUE_ONLY"),
                sick=lambda r: r.update(during_probation="BLOCK"),
                casual=lambda r: r.setdefault("eligibility", {}).update(
                    min_tenure={"value": 36, "unit": "MONTHS", "basis": "HIRE_DATE", "mode": "BLOCK_USE"}))
    ok(acme["admin"].put(f"{L}/employees/{acme['worker_id']}/lifecycle", json={"probation_end_date": "2026-11-30"}))
    crossing = preview(worker, "earned", "2026-11-30", "2026-12-01")
    issue = next(i for i in crossing["issues"] if i["code"] == "NOT_ELIGIBLE")
    assert issue["details"] == {"date": "2026-11-30", "check": "PROBATION", "available_from": "2026-12-01"}
    assert not errors(preview(worker, "earned", "2026-12-01", "2026-12-02"))  # after probation: bookable
    blocked = preview(worker, "sick", "2026-11-02")
    assert errors(blocked) == ["NOT_ELIGIBLE"] and blocked["issues"][0]["details"]["check"] == "PROBATION"
    cards = {b["leave_type_code"]: b for b in ok(worker.get(f"{L}/balances/me"))["balances"]}
    assert (cards["earned"]["eligibility"], cards["earned"]["available_from"]) == ("ACCRUE_ONLY", "2026-12-01")
    assert cards["sick"]["eligibility"] == "NOT_ELIGIBLE"
    # Min tenure 36 months from the hire date (2024-01-15), BLOCK_USE: accrues, usable from 2027-01-15.
    tenure = preview(peer, "casual", "2026-11-02")
    detail = next(i for i in tenure["issues"] if i["code"] == "NOT_ELIGIBLE")["details"]
    assert (detail["check"], detail["available_from"]) == ("TENURE", "2027-01-15")
    refused = apply(peer, "casual", "2026-11-02")
    assert (refused.status_code, refused.json()["code"]) == (422, "NOT_ELIGIBLE")


# ------------------------------------------------------------------------------------------- charging (SPINE §6.1-§6.2)
def test_lh_req_sandwich_weekly_offs_inside_and_at_the_edge_and_calendar_days_basis(acme):
    worker = acme["worker"]
    go_live(acme)
    custom_plan(acme, casual=lambda r: (r.update(sandwich={"mode": "WEEKLY_OFFS", "half_day_edges": "IGNORE"}),
                                        request_rules(r, max_units=None, caps=[])),
                earned=lambda r: r.update(counting_basis={"basis": "CALENDAR_DAYS"}))
    inside = preview(worker, "casual", "2026-11-06", "2026-11-09")  # Fri .. Mon
    assert inside["charged_units"] == 4
    assert [d["charge_reason"] for d in inside["days"]] == ["WORKING", "SANDWICH", "SANDWICH", "WORKING"]
    assert preview(worker, "sick", "2026-11-06", "2026-11-09")["charged_units"] == 2  # sick: sandwich OFF
    friday = ok(apply(worker, "casual", "2026-11-13"), 201)
    assert approve_all(acme, friday["id"]) == "approved"
    edge = preview(worker, "casual", "2026-11-14", "2026-11-16")  # Sat .. Mon next to Friday's leave
    assert (edge["charged_units"], [d["charge_reason"] for d in edge["days"]]) == (3, ["SANDWICH", "SANDWICH",
                                                                                       "WORKING"])
    assert preview(worker, "casual", "2026-11-16")["charged_units"] == 1  # the weekend is not in this request
    sick = ok(apply(acme["peer"], "sick", "2026-11-20"), 201)  # sick has no sandwich: it never closes one
    ok(acme["boss"].post(f"{L}/requests/{sick['id']}/approve"))
    assert preview(acme["peer"], "casual", "2026-11-21", "2026-11-23")["charged_units"] == 1
    monday = ok(apply(worker, "casual", "2026-11-14", "2026-11-16"), 201)
    assert approve_all(acme, monday["id"]) == "approved"
    rows = owner_rows("SELECT date::text AS d, charge_reason AS r, counts_against_balance AS c FROM leave_days "
                      "WHERE leave_id = :i AND voided_at IS NULL ORDER BY date", i=monday["id"])
    assert [(r["d"], r["r"], r["c"]) for r in rows] == [("2026-11-14", "SANDWICH", True),
                                                       ("2026-11-15", "SANDWICH", True),
                                                       ("2026-11-16", "WORKING", True)]
    assert balance(worker, "casual")["balance"] == 12 - 1 - 3
    week = preview(worker, "earned", "2026-11-23", "2026-11-29")  # CALENDAR_DAYS: Mon .. Sun = 7
    assert week["charged_units"] == 7 and [d["charge_reason"] for d in week["days"]][-2:] == ["BASIS", "BASIS"]


def test_lh_bal_23_holiday_inside_a_request_and_holiday_added_after_approval(acme, monkeypatch):
    """A PUBLIC holiday inside a WORKING_DAYS request is not charged; a COMPANY holiday added after approval on a
    v2 tenant recounts the leave (version + 1, ledger credit, SYSTEM_EVENT)."""
    from app.services import holiday_years
    from tests.holiday.test_day_facts_v2 import HC, _calendar, _link, _published

    def _amend(client, calendar_id, year, operations, reason):  # dry run, then acknowledge its warnings
        url = f"{HC}/{calendar_id}/years/{year}/amend"
        dry = ok(client.post(url, json={"operations": operations, "reason": reason, "dry_run": True}))
        acks = [w["code"] for w in dry["impact"]["warnings"] if w.get("confirm")]
        return ok(client.post(url, json={"operations": operations, "reason": reason, "acknowledged_warnings": acks}))

    monkeypatch.setattr(holiday_years, "require_publish_step_up", lambda db, principal: None)
    admin, worker = acme["admin"], acme["worker"]
    go_live(acme)
    branch = owner_rows("SELECT branch_id FROM employee_assignments WHERE employee_id = :i AND is_primary",
                        i=acme["worker_id"])[0]["branch_id"]
    cal = _calendar(admin, "Acme India", country_code="IN")
    _published(admin, cal["id"], [("Guru Nanak Jayanti", "2026-11-11", "PUBLIC")])
    _link(admin, branch, holiday_calendar_id=cal["id"])
    leave = ok(apply(worker, "casual", "2026-11-09", "2026-11-13"), 201)  # Mon .. Fri, Wed is a holiday
    assert leave["number_of_days"] == 4
    assert approve_all(acme, leave["id"]) == "approved"
    assert balance(worker, "casual")["balance"] == 6
    _amend(admin, cal["id"], 2026, [{"op": "add", "name": "Founders Day", "date": "2026-11-13",
                                     "holiday_type": "COMPANY", "status": "CONFIRMED"}], "Office closed on Friday")
    after = ok(worker.get(f"{L}/requests/{leave['id']}"))
    assert (after["number_of_days"], after["version"]) == (3, 2)
    assert [d["date"] for d in after["days"] if d["counted"]] == ["2026-11-09", "2026-11-10", "2026-11-12"]
    deltas = sorted(Decimal(r["delta"]) for r in owner_rows(
        "SELECT delta FROM leave_balance_transactions WHERE ref_type = 'leave' AND ref_id = :i", i=leave["id"]))
    assert deltas == [Decimal("-4.00"), Decimal("1.00")]
    assert balance(worker, "casual")["balance"] == 7
    messages = ok(worker.get(f"{L}/requests/{leave['id']}/messages"))
    assert any(m["kind"] == "SYSTEM_EVENT" and "from 4 to 3" in m["body"] for m in messages)


# ------------------------------------------------------------------------------------------- request rules (LH-REQ)
def test_lh_req_caps_per_month_across_a_month_end_max_instances_and_clubbing(acme):
    worker, peer = acme["worker"], acme["peer"]
    go_live(acme)
    custom_plan(acme, casual=lambda r: request_rules(
        r, max_instances=[{"period": "MONTH", "max_requests": 2}],
        clubbing={"not_with": ["sick"], "gap_days": 0}))
    ok(apply(worker, "casual", "2026-12-07", "2026-12-08"), 201)  # 2 of December's 3
    crossing = apply(worker, "casual", "2026-11-30", "2026-12-02")  # 1 in November, 2 in December
    assert (crossing.status_code, crossing.json()["code"]) == (422, "MAX_UNITS")
    assert (crossing.json()["details"]["period_start"], crossing.json()["details"]["used"]) == ("2026-12-01", 2)
    ok(apply(worker, "casual", "2026-11-02"), 201)
    ok(apply(worker, "casual", "2026-11-04"), 201)
    third = apply(worker, "casual", "2026-11-06")
    assert (third.status_code, third.json()["code"]) == (422, "MAX_INSTANCES")
    # Clubbing: casual may not touch sick leave, also across a weekend the sick request covers.
    ok(apply(peer, "sick", "2026-11-20"), 201)
    assert errors(preview(peer, "casual", "2026-11-23")) == ["CLUBBING_NOT_ALLOWED"]
    ok(apply(peer, "sick", "2026-11-27", "2026-11-29"), 201)  # Fri .. Sun
    assert errors(preview(peer, "casual", "2026-11-30")) == ["CLUBBING_NOT_ALLOWED"]
    assert errors(preview(peer, "casual", "2026-11-25")) == []


def test_lh_bal_10_allow_negative_limit_and_lh_bal_18_projected_check(acme):
    hr, worker = acme["hr"], acme["worker"]
    go_live(acme)
    custom_plan(acme, casual=lambda r: (r.update(insufficient_balance_action="ALLOW_NEGATIVE",
                                                 negative_allowance="2"), request_rules(r, max_units=None, caps=[])),
                earned=lambda r: r.update(balance_check="PROJECTED"))
    ok(hr.post(f"{L}/adjustments", json={"employee_id": acme["worker_id"], "leave_type": "casual",
                                         "delta": "-11.5", "effective_date": "2026-10-19",
                                         "reason": "Leave taken before the system"}), 201)
    assert balance(worker, "casual")["available"] == 0.5
    at_limit = preview(worker, "casual", "2026-11-02", "2026-11-04", end_portion="FIRST_HALF")  # 2.5
    assert (at_limit["charged_units"], at_limit["balance_after"], at_limit["blocking"]) == (2.5, -2, False)
    assert [i["code"] for i in at_limit["issues"] if i["severity"] == "WARNING"] == ["NEGATIVE_BALANCE"]
    beyond = preview(worker, "casual", "2026-11-02", "2026-11-04")
    assert errors(beyond) == ["NEGATIVE_LIMIT"]
    ok(apply(worker, "casual", "2026-11-02", "2026-11-04", end_portion="FIRST_HALF"), 201)
    assert balance(worker, "casual")["available"] == -2  # shown unclamped
    # PROJECTED: 13.5 today + the October and November accruals (posted at the month ends) = 16.5 for December.
    assert balance(worker, "earned")["available"] == 13.5
    fifteen = preview(worker, "earned", "2026-12-01", "2026-12-21")
    assert (fifteen["charged_units"], fifteen["balance_before"], fifteen["blocking"]) == (15, 16.5, False)
    seventeen = preview(worker, "earned", "2026-12-01", "2026-12-23")
    assert errors(seventeen) == ["INSUFFICIENT_BALANCE"]


def test_lh_sec_09_ten_parallel_submits_never_overdraw_a_small_balance(app, acme):
    from app.core.rate_limit import limiter

    hr, worker = acme["hr"], acme["worker"]
    go_live(acme)
    ok(hr.post(f"{L}/adjustments", json={"employee_id": acme["worker_id"], "leave_type": "casual", "delta": "-7",
                                         "effective_date": "2026-10-19", "reason": "Opening correction"}), 201)
    clients = []
    for _ in range(10):
        limiter.reset()
        clients.append(login(app, worker.email))
    windows = [("2026-11-02", "2026-11-03"), ("2026-11-04", "2026-11-05"), ("2026-11-09", "2026-11-10"),
               ("2026-11-11", "2026-11-12"), ("2026-11-16", "2026-11-17"), ("2026-11-18", "2026-11-19"),
               ("2026-11-23", "2026-11-24"), ("2026-11-25", "2026-11-26"), ("2026-11-30", "2026-12-01"),
               ("2026-12-02", "2026-12-03")]
    results = []

    def go(window, client):
        r = client.post(f"{L}/requests", headers={"Idempotency-Key": uuid.uuid4().hex},
                        json={"leave_type": "casual", "start_date": window[0], "end_date": window[1]})
        results.append("OK" if r.status_code == 201 else r.json().get("code"))

    threads = [threading.Thread(target=go, args=(w, c)) for w, c in zip(windows, clients)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("OK") == 1 and set(results) <= {"OK", "INSUFFICIENT_BALANCE"}, results
    card = balance(worker, "casual")
    assert (card["pending"], card["available"]) == (2, 1)


def test_lh_req_10_split_sibling_follows_return_and_resubmit(acme):
    worker, boss = acme["worker"], acme["boss"]
    go_live(acme)
    custom_plan(acme, casual=lambda r: (r.update(insufficient_balance_action="SPLIT_TO_UNPAID"),
                                        request_rules(r, max_units=None, caps=[])))
    created = ok(apply(worker, "casual", "2026-11-09", "2026-11-27", reason="Wedding"), 201)  # 15 days, 12 available
    assert [(s["leave_type"], s["number_of_days"], s["status"]) for s in created["siblings"]] == [
        ("unpaid", 3, "pending")]
    ok(boss.post(f"{L}/requests/{created['id']}/return", json={"reason": "Talk to me about the dates"}))
    assert [s["status"] for s in ok(worker.get(f"{L}/requests/{created['id']}"))["siblings"]] == ["returned"]
    resubmitted = ok(worker.post(f"{L}/requests/{created['id']}/submit"))  # unchanged: the same 12 + 3 split
    assert (resubmitted["number_of_days"], resubmitted["end_date"]) == (12, "2026-11-24")
    assert sorted(s["status"] for s in resubmitted["siblings"]) == ["pending", "withdrawn"]
    ok(boss.post(f"{L}/requests/{created['id']}/return", json={"reason": "Still too long"}))
    # Edited back to the full range: the edit replaces the old split; the resubmission makes a new one.
    ok(worker.put(f"{L}/requests/{created['id']}", json={"leave_type": "casual", "start_date": "2026-11-09",
                                                          "end_date": "2026-11-27", "reason": "Wedding"}))
    ok(worker.post(f"{L}/requests/{created['id']}/submit"))
    siblings = ok(worker.get(f"{L}/requests/{created['id']}"))["siblings"]
    assert sorted(s["status"] for s in siblings) == ["pending", "withdrawn", "withdrawn"]
    assert approve_all(acme, created["id"]) == "approved"  # one live sibling: no slot collision
    live = [s for s in ok(worker.get(f"{L}/requests/{created['id']}"))["siblings"] if s["status"] != "withdrawn"]
    assert [(s["status"], s["number_of_days"]) for s in live] == [("approved", 3)]
    assert balance(worker, "casual")["balance"] == 0


def test_lh_req_21_returned_request_expires_and_sla_reminds_once(acme):
    from app.services import leave_sla

    worker, boss, cid = acme["worker"], acme["boss"], acme["cid"]
    go_live(acme)
    returned = ok(apply(worker, "casual", "2026-10-26"), 201)
    ok(boss.post(f"{L}/requests/{returned['id']}/return", json={"reason": "Pick another day"}))
    _sql("UPDATE leaves SET returned_at = '2026-10-23 06:00+00' WHERE id = :i", i=returned["id"])  # Fri 23 Oct
    waiting = ok(apply(worker, "casual", "2026-11-02"), 201)
    _sql("UPDATE leaves SET submitted_at = now() - interval '3 days' WHERE id = :i", i=waiting["id"])
    at_ist(acme, "2026-11-02", "10:00")  # expiry = min(23 Oct + 14, max(26 Oct, 23 Oct) + 7) = Mon 2 Nov
    with tenant(cid) as db:
        first = leave_sla.scan(db, cid)
    assert (first["reminded"], first["returned_expired"]) == (1, 0)
    with tenant(cid) as db:
        assert leave_sla.scan(db, cid)["reminded"] == 0  # once per step
    at_ist(acme, "2026-11-03", "01:00")
    with tenant(cid) as db:
        assert leave_sla.scan(db, cid)["returned_expired"] == 1
    detail = ok(worker.get(f"{L}/requests/{returned['id']}"))
    assert detail["status_v2"] == "WITHDRAWN"
    assert ok(worker.get(f"{L}/requests/{waiting['id']}"))["status_v2"] == "PENDING"
    reminders = owner_rows("SELECT count(*) AS n FROM notifications WHERE user_id = :u AND title = :t",
                           u=boss.user_id, t="Leave request waiting for your decision")
    assert reminders[0]["n"] == 1


# ------------------------------------------------------------------------------------------- comp-off and lots
def test_lh_cmp_29_revoke_reverses_only_the_unused_part_and_lots_expire_once(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    go_live(acme)
    office_plan(acme)
    for day in ("2026-10-17", "2026-10-18"):  # Sat and Sun, 8.5 h each
        _sql("INSERT INTO attendance (company_id, employee_id, check_in_time, check_out_time, date, source, status, "
             "is_manual, created_at, updated_at) VALUES (:c, :e, CAST(:d AS date) + time '03:30', "
             "CAST(:d AS date) + time '12:00', CAST(:d AS date), 'manual', 'present', true, now(), now())",
             c=acme["cid"], e=acme["worker_id"], d=day)
    claims = [ok(worker.post(f"{L}/comp-off/claims", json={"worked_date": day}), 201)
              for day in ("2026-10-17", "2026-10-18")]
    approved = [ok(boss.post(f"{L}/comp-off/claims/{c['id']}/approve")) for c in claims]
    assert [a["expires_on"] for a in approved] == ["2026-12-16", "2026-12-17"]
    half = ok(apply(worker, "comp_off", "2026-10-26", end_portion="FIRST_HALF"), 201)
    assert half["number_of_days"] == 0.5
    ok(boss.post(f"{L}/requests/{half['id']}/approve"))
    assert balance(worker, "comp_off")["balance"] == 1.5
    # The first lot (earliest expiry) paid the half day: revoking it reverses only its unused 0.5.
    revoked = ok(hr.post(f"{L}/comp-off/claims/{claims[0]['id']}/revoke", json={"remarks": "Attendance corrected"}))
    assert revoked["status"] == "REVOKED"
    assert balance(worker, "comp_off")["balance"] == 1
    rows = owner_rows("SELECT delta FROM leave_balance_transactions WHERE company_id = :c AND reason = 'EXPIRE'",
                      c=acme["cid"])
    assert [Decimal(r["delta"]) for r in rows] == [Decimal("-0.50")]
    at_ist(acme, "2026-12-18", "09:00")
    run = ok(hr.post(f"{L}/accruals/run"))
    assert run["expiries"] == 1
    assert ok(hr.post(f"{L}/accruals/run"))["expiries"] == 0  # once per lot
    assert balance(worker, "comp_off")["balance"] == 0
    rows = owner_rows("SELECT delta FROM leave_balance_transactions WHERE company_id = :c AND reason = 'EXPIRE' "
                      "ORDER BY id", c=acme["cid"])
    assert [Decimal(r["delta"]) for r in rows] == [Decimal("-0.50"), Decimal("-1.00")]


def test_lh_ye_07_year_end_carry_cap_lapse_and_expiring_carried_lot(acme):
    hr, hr2, worker = acme["hr"], acme["hr2"], acme["worker"]
    go_live(acme)
    custom_plan(acme, casual=lambda r: r.update(carry_forward={"cap": "2", "expiry_months": 3}))
    one = ok(apply(worker, "casual", "2026-11-02"), 201)
    ok(acme["boss"].post(f"{L}/requests/{one['id']}/approve"))
    assert balance(worker, "casual")["balance"] == 11
    at_ist(acme, "2027-01-04", "09:00")
    run = ok(hr.post(f"{L}/year-end", json={"cycle_end": "2026-12-31"}), 201)
    row = next(r for r in run["rows"] if r["employee_id"] == acme["worker_id"] and r["leave_type"] == "casual")
    assert (Decimal(row["carry"]), Decimal(row["lapse"])) == (Decimal(2), Decimal(9))
    step_up(acme["cid"])
    ok(hr2.post(f"{L}/year-end/{run['id']}/post"))
    ok(hr.post(f"{L}/accruals/run"))
    card = balance(worker, "casual")
    assert (card["cycle_start"], card["opening"], card["credited"], card["balance"]) == ("2027-01-01", 2, 12, 14)
    assert card["expiring"] == [{"units": 2, "expires_on": "2027-03-31"}]
    at_ist(acme, "2027-04-02", "09:00")
    assert ok(hr.post(f"{L}/accruals/run"))["expiries"] >= 1
    assert ok(hr.post(f"{L}/accruals/run"))["expiries"] == 0
    card = balance(worker, "casual")
    assert (card["balance"], card["expired"]) == (12, 2)
    expired = owner_rows("SELECT delta, effective_date::text AS d FROM leave_balance_transactions WHERE company_id = "
                         ":c AND employee_id = :e AND leave_type = 'casual' AND reason = 'EXPIRE'",
                         c=acme["cid"], e=acme["worker_id"])
    assert [(Decimal(r["delta"]), r["d"]) for r in expired] == [(Decimal("-2.00"), "2027-04-01")]


def test_lh_bal_21_encashment_on_request_step_floor_cap_reservation_and_payroll_note(acme):
    worker, boss = acme["worker"], acme["boss"]
    go_live(acme)
    custom_plan(acme, earned=lambda r: r.update(encashment={
        "eligible": True, "triggers": ["EXIT", "YEAR_END", "ON_REQUEST"], "max_units_per_cycle": "5",
        "min_balance_after": "8", "step": "0.5"}))
    assert balance(worker, "earned")["available"] == 13.5

    def encash(units):
        return worker.post(f"{L}/encashments", json={"leave_type": "earned", "units": units})

    assert encash("0.3").json()["code"] == "RULE_INVALID"  # steps of 0.5
    floor = encash("6")  # 13.5 - 8 = 5.5 may be encashed
    assert (floor.status_code, floor.json()["code"], floor.json()["details"]["min_balance_after"]) == (
        400, "INSUFFICIENT_BALANCE", 8)
    first = ok(encash("3"), 201)
    card = balance(worker, "earned")
    assert (card["reserved_encashment"], card["available"]) == (3, 10.5)  # the reservation reduces available
    assert encash("2.5").json()["code"] == "MAX_UNITS"  # 3 + 2.5 > 5 per cycle
    second = ok(encash("2"), 201)
    no_reason = boss.post(f"{L}/encashments/{second['id']}/reject", json={})
    assert no_reason.json()["code"] == "REASON_REQUIRED"
    rejected = ok(boss.post(f"{L}/encashments/{second['id']}/reject", json={"remarks": "Not this quarter"}))
    assert rejected["status"] == "REJECTED" and balance(worker, "earned")["available"] == 10.5
    approved = ok(boss.post(f"{L}/encashments/{first['id']}/approve"))
    assert approved["status"] == "APPROVED" and approved["payroll_reference"] is None
    assert "Payroll hand-off pending" in (approved["decision_note"] or "")  # no pay group yet: HR retries
    assert balance(worker, "earned")["available"] == 10.5  # still reserved until payroll pays it


# ------------------------------------------------------------------------------------------- HR balance tools
def test_lh_sec_22_adjustment_step_up_threshold_and_own_balance(acme):
    hr, worker = acme["hr"], acme["worker"]
    go_live(acme)
    _sql("UPDATE user_sessions SET step_up_at = NULL WHERE user_id IN (SELECT id FROM users WHERE company_id = :c)",
         c=acme["cid"])

    def adjust(employee_id, delta, leave_type="casual"):
        return hr.post(f"{L}/adjustments", json={"employee_id": employee_id, "leave_type": leave_type, "delta": delta,
                                                 "effective_date": "2026-10-19", "reason": "Correction"})

    assert adjust(acme["worker_id"], "10").status_code == 201  # exactly the threshold: no step-up
    large = adjust(acme["worker_id"], "10.5")
    assert (large.status_code, large.json()["code"]) == (403, "STEP_UP_REQUIRED")
    own = adjust(hr.employee["id"], "1")
    assert own.status_code == 403
    step_up(acme["cid"])
    assert adjust(acme["worker_id"], "10.5").status_code == 201
    assert balance(worker, "casual")["balance"] == 30.5
    ok(hr.post(f"{L}/v2/types", json={"code": "bereave_ev", "name": "Bereavement", "category": "BEREAVEMENT",
                                      "kind": "EVENT"}), 201)
    event_type = adjust(acme["worker_id"], "1", leave_type="bereave_ev")
    assert (event_type.status_code, event_type.json()["code"]) == (400, "RULE_INVALID")


def test_lh_imp_02_opening_import_dry_run_errors_commit_and_second_import(acme):
    hr, worker, peer = acme["hr"], acme["worker"], acme["peer"]
    go_live(acme)
    worker_code, peer_code = worker.employee["employee_code"], peer.employee["employee_code"]
    header = "employee_code,leave_type,units,effective_date,expires_on,note\n"

    def upload(text):
        return ok(hr.post(f"{L}/imports", data={"kind": "OPENING_BALANCES"},
                          files={"file": ("opening.csv", (header + text).encode(), "text/csv")}), 201)

    dirty = upload(f"{worker_code},earned,5,2026-10-01,,carried in\nNOPE,earned,1,2026-10-01,,\n"
                   f"{peer_code},earned,abc,2026-10-01,,\n")
    assert (dirty["report"]["valid"], dirty["report"]["error_count"]) == (1, 2)
    assert [e["line"] for e in dirty["report"]["errors"]] == [3, 4]
    refused = hr.post(f"{L}/imports/{dirty['id']}/commit")
    assert (refused.status_code, refused.json()["code"]) == (409, "IMPORT_HAS_ERRORS")
    clean = upload(f"{worker_code},earned,5,2026-10-01,,carried in\n")
    committed = ok(hr.post(f"{L}/imports/{clean['id']}/commit"))
    assert (committed["status"], committed["report"]["posted"]) == ("COMMITTED", 1)
    assert balance(worker, "earned")["opening"] == 5
    assert hr.post(f"{L}/imports/{clean['id']}/commit").json()["code"] == "INVALID_TRANSITION"
    again = upload(f"{worker_code},earned,5,2026-10-01,,\n{peer_code},earned,2,2026-10-01,,\n"
                   f"{peer_code},earned,3,2026-10-02,,\n")
    codes = {e["line"]: e["codes"] for e in again["report"]["errors"]}
    assert codes == {2: ["OPENING_ALREADY_POSTED"], 4: ["DUPLICATE_ROW"]}


# ------------------------------------------------------------------------------------------- LEGACY_V1 tenant
def test_lh_mig_legacy_v1_tenant_is_unaffected_before_cutover(acme):
    hr, worker, boss = acme["hr"], acme["worker"], acme["boss"]
    before = ok(worker.get(f"{L}/my/balance"))
    office_plan(acme)  # built and activated, but inert until go-live (it adds a comp-off type with no quota)
    now = ok(worker.get(f"{L}/my/balance"))
    assert {code: now[code] for code in before} == before
    v2_cards = {b["leave_type_code"]: b for b in ok(worker.get(f"{L}/balances/me"))["balances"]}
    assert (v2_cards["casual"]["available"], v2_cards["casual"]["events"]) == (before["casual"]["available"], [])
    legacy = ok(worker.post(f"{L}/apply", json={"leave_type": "casual", "start_date": "2026-11-02",
                                                "end_date": "2026-11-03"}))
    ok(boss.post(f"{L}/{legacy['id']}/approve"))
    after = ok(worker.get(f"{L}/my/balance"))["casual"]
    assert (after["used"], after["available"]) == (2, before["casual"]["available"] - 2)
    keys = [r["k"] for r in owner_rows("SELECT idempotency_key AS k, reason FROM leave_balance_transactions WHERE "
                                       "company_id = :c", c=acme["cid"])]
    assert keys == [f"leave:{legacy['id']}:use:2026"]  # legacy writer, no GRANT rows
    for path, body in ((f"{L}/adjustments", {"employee_id": acme["worker_id"], "leave_type": "casual", "delta": "1",
                                             "effective_date": "2026-10-19", "reason": "No v2 yet"}),
                       (f"{L}/events", {"leave_type_code": "maternity", "event_type": "BIRTH",
                                        "event_date": "2026-10-10"}),
                       (f"{L}/comp-off/claims", {"worked_date": "2026-10-17"})):
        assert hr.post(path, json=body).status_code == 404, path
