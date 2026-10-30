"""EVENT leaves (03-leave/06 §6.1; SPINE §6.1, V3-053(7)) end to end through the API on PostgreSQL.

Fake clock Mon 2026-10-19 09:00 IST (tests/leave/test_v2_core.py fixtures). The seeded ``maternity`` / ``paternity``
types are referenced by the seeded pay policy (their kind is frozen, BUG-23), so after go-live HR creates two EVENT
types and adds them to the India office plan: ``maternity_ev`` 26 weeks (182 calendar days) from 56 days before the
expected delivery, at most 3 events per employment, women only; ``paternity_ev`` 5 working days within 90 days of the
birth (several requests may draw on one event).
"""

from datetime import date, timedelta
from decimal import Decimal

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.leave.test_v2_core import (  # noqa: F401  (acme is a fixture)
    L, acme, apply, backdate_settings, balance, go_live, ok, office_plan,
)

E = f"{L}/events"
MAT, PAT = "maternity_ev", "paternity_ev"
_EXEMPT = {"blackouts": "EXEMPT", "coverage": "EXEMPT"}
MATERNITY = {"leave_type_code": MAT, "balance_scope": {"kind": "EVENT"}, "counting_basis": {"basis": "CALENDAR_DAYS"},
             "entitlement": {"method": "EVENT_GRANT"}, "carry_forward": {"cap": "0"},
             "eligibility": {"genders": ["FEMALE"]}, "request": _EXEMPT,
             "event": {"event_types": ["EXPECTED_DELIVERY", "BIRTH", "ADOPTION_HANDOVER"], "entitlement_units": "26",
                       "duration_unit": "WEEKS", "earliest_start_offset_days": -56, "max_occurrences": 3}}
PATERNITY = {"leave_type_code": PAT, "balance_scope": {"kind": "EVENT"}, "entitlement": {"method": "EVENT_GRANT"},
             "carry_forward": {"cap": "0"}, "request": _EXEMPT,
             "event": {"event_types": ["BIRTH", "ADOPTION_HANDOVER"], "entitlement_units": "5",
                       "duration_unit": "WORKING_DAYS", "take_within_days": 90}}


def live_with_events(t):
    """Go live, create the two EVENT types and an active India office plan that includes them."""
    hr = t["hr"]
    go_live(t)
    ok(hr.post(f"{L}/v2/types", json={"code": MAT, "name": "Maternity Leave", "category": "PARENTAL", "kind": "EVENT",
                                      "counts_non_working_days": True}), 201)
    ok(hr.post(f"{L}/v2/types", json={"code": PAT, "name": "Paternity Leave", "category": "PARENTAL",
                                      "kind": "EVENT"}), 201)
    plan = office_plan(t, activate=False)
    fields = ok(hr.get(f"{L}/plans/{plan['id']}"))["versions"][0]["fields"]
    fields["types"] += [MATERNITY, PATERNITY]
    ok(hr.put(f"{L}/plans/{plan['id']}/versions/1", json={"fields": fields}))
    ok(hr.post(f"{L}/plans/{plan['id']}/versions/1/activate", json={"effective_from": "2026-01-01",
                                                                     "reason": "Office plan with event leave"}))


def event(client, leave_type=MAT, event_type="EXPECTED_DELIVERY", event_date="2027-01-04", **extra):
    return client.post(E, json={"leave_type_code": leave_type, "event_type": event_type, "event_date": event_date,
                                **extra})


def approve_all(t, leave_id):
    for approver in (t["boss"], t["hr"]):
        status = owner_rows("SELECT status FROM leaves WHERE id = :i", i=leave_id)[0]["status"]
        if status != "pending":
            break
        ok(approver.post(f"{L}/requests/{leave_id}/approve", json={"remarks": "ok"}))
    return owner_rows("SELECT status, number_of_days FROM leaves WHERE id = :i", i=leave_id)[0]


def used_units(event_id):
    return Decimal(owner_rows("SELECT used_units FROM leave_events WHERE id = :i", i=event_id)[0]["used_units"])


# ------------------------------------------------------------------------------------------- maternity end to end
def test_lh_evt_03_maternity_event_calendar_days_one_request_and_no_ledger_rows(acme):
    worker, hr = acme["worker"], acme["hr"]
    live_with_events(acme)
    created = ok(event(worker), 201)
    assert (created["status"], created["entitled"], created["remaining"], created["earliest_start"]) == (
        "OPEN", 182, 182, "2026-11-09")
    card = balance(worker, MAT)
    assert (card["scope"], card["credited"], card["balance"], card["available"]) == ("EVENT", 182, 182, 182)
    assert [(e["event_id"], e["remaining"]) for e in card["events"]] == [(created["id"], 182)]
    # LH-EVT-01: an EVENT type needs an open event of the employee.
    missing = apply(worker, MAT, "2026-11-09", "2027-05-09")
    assert (missing.status_code, missing.json()["code"]) == (422, "EVENT_REQUIRED")
    # LH-EVT-03: 2 Nov is before 4 Jan - 56 days.
    early = apply(worker, MAT, "2026-11-02", "2027-05-02", event_id=created["id"])
    assert (early.status_code, early.json()["code"]) == (422, "EVENT_WINDOW_CLOSED")
    assert early.json()["details"]["reason"] == "TOO_EARLY"
    preview = ok(worker.post(f"{L}/preview", json={"leave_type": MAT, "start_date": "2026-11-09",
                                                   "end_date": "2027-05-09", "event_id": created["id"]}))
    assert preview["charged_units"] == 182 and not preview["blocking"]  # one charge, not split at 1 Jan
    assert preview["event"]["suggested_end_date"] == "2027-05-09" and preview["event"]["remaining_after"] == 0
    leave = ok(apply(worker, MAT, "2026-11-09", "2027-05-09", event_id=created["id"]), 201)
    assert leave["event_id"] == created["id"] and leave["number_of_days"] == 182
    # PENDING reserves the units: a second request is refused, and nothing is used yet.
    pending_card = balance(worker, MAT)
    assert (pending_card["pending"], pending_card["available"], pending_card["used"]) == (182, 0, 0)
    again = apply(worker, MAT, "2027-05-10", "2027-05-12", event_id=created["id"])
    assert (again.status_code, again.json()["code"]) == (400, "INSUFFICIENT_BALANCE")
    assert again.json()["details"]["available"] == 0
    assert approve_all(acme, leave["id"])["status"] == "approved"
    days = owner_rows("SELECT count(*) AS n, sum(units) AS u, bool_and(counts_against_balance) AS c, "
                      "min(counting_basis) AS b FROM leave_days WHERE leave_id = :i AND voided_at IS NULL",
                      i=leave["id"])[0]
    assert (days["n"], Decimal(days["u"]), days["c"], days["b"]) == (182, Decimal(182), True, "CALENDAR_DAYS")
    assert used_units(created["id"]) == 182
    assert owner_rows("SELECT count(*) AS n FROM leave_balance_transactions WHERE company_id = :c AND leave_type = "
                      "'maternity_ev'", c=acme["cid"])[0]["n"] == 0  # EVENT_GRANT: no GRANT, no USE rows
    card = balance(worker, MAT)
    assert (card["used"], card["pending"], card["balance"], card["available"]) == (182, 0, 0, 0)
    beyond = apply(worker, MAT, "2027-05-10", "2027-05-10", event_id=created["id"])
    assert beyond.json()["code"] == "INSUFFICIENT_BALANCE"
    # LH-EVT-16: the event has an approved request.
    in_use = worker.post(f"{E}/{created['id']}/cancel", json={"reason": "recorded in error"})
    assert (in_use.status_code, in_use.json()["code"]) == (409, "EVENT_IN_USE")
    # Early return: PARTIAL_CANCEL of the last 9 days gives 9 units back to the event, once.
    tail = [(date(2027, 5, 1) + timedelta(days=n)).isoformat() for n in range(9)]
    change = ok(worker.post(f"{L}/requests/{leave['id']}/changes", json={"kind": "PARTIAL_CANCEL",
                                                                         "proposed": {"dates": tail}}), 201)
    ok(acme["boss"].post(f"{L}/changes/{change['id']}/approve"))
    assert used_units(created["id"]) == 173
    assert balance(worker, MAT)["available"] == 9
    # One continuous request per CALENDAR_DAYS event: the remaining 9 days are not a second request.
    second = apply(worker, MAT, "2027-05-03", "2027-05-04", event_id=created["id"])
    assert second.json()["code"] == "MAX_INSTANCES" and second.json()["details"]["detail"] == "ONE_REQUEST_PER_EVENT"
    # HR cancels the leave (applies at once), then the event can be cancelled.
    ok(hr.post(f"{L}/requests/{leave['id']}/changes", json={"kind": "CANCEL", "reason": "Recorded in error"}), 201)
    assert used_units(created["id"]) == 0
    cancelled = ok(worker.post(f"{E}/{created['id']}/cancel", json={"reason": "recorded in error"}))
    assert cancelled["status"] == "CANCELLED"
    assert balance(worker, MAT)["events"] == []
    assert owner_rows("SELECT count(*) AS n FROM leave_balance_transactions WHERE company_id = :c AND leave_type = "
                      "'maternity_ev'", c=acme["cid"])[0]["n"] == 0


def test_lh_evt_04_lh_evt_10_duplicate_event_and_max_occurrences(acme):
    worker = acme["worker"]
    live_with_events(acme)
    first = ok(event(worker), 201)
    duplicate = event(worker, event_date="2027-02-01")
    assert (duplicate.status_code, duplicate.json()["code"]) == (409, "PLAN_CONFLICT")
    assert duplicate.json()["details"] == {"reason": "DUPLICATE_EVENT", "event_id": first["id"]}
    wrong_kind = event(worker, event_type="DEATH")
    assert (wrong_kind.status_code, wrong_kind.json()["code"]) == (422, "INVALID_FIELDS")
    ok(event(worker, event_type="BIRTH", event_date="2026-10-01"), 201)
    third = ok(event(worker, event_type="ADOPTION_HANDOVER", event_date="2027-03-01"), 201)
    fourth = event(worker, event_date="2027-08-01")  # > 180 days after the first expected delivery
    assert (fourth.status_code, fourth.json()["code"]) == (422, "MAX_INSTANCES")
    assert fourth.json()["details"]["limit"] == 3
    ok(worker.post(f"{E}/{third['id']}/cancel"))  # recorded in error: no longer counts
    assert ok(event(worker, event_date="2027-08-01"), 201)["status"] == "OPEN"
    mine = ok(worker.get(E))
    assert sorted(e["status"] for e in mine) == ["CANCELLED", "OPEN", "OPEN", "OPEN"]


def test_lh_elg_02_lh_elg_07_gender_rule_never_echoed_and_unknown_gender_is_verified(app, acme):
    admin = acme["admin"]
    karan = create_employee(admin, "Karan", "Malhotra", manager_id=acme["boss_id"], gender="Male")
    karan_client = invite_and_activate(app, admin, karan, ["employee"])
    live_with_events(acme)
    refused = event(karan_client)
    assert (refused.status_code, refused.json()["code"]) == (422, "NOT_ELIGIBLE")
    text = refused.text.upper()
    assert "GENDER" not in text and "MALE" not in text and refused.json().get("details") is None
    assert all(b["leave_type_code"] != MAT for b in ok(karan_client.get(f"{L}/balances/me"))["balances"])
    # Ravi's gender is not recorded: eligible, flagged for HR (never silently refused).
    peer = acme["peer"]
    created = ok(event(peer), 201)
    leave = ok(apply(peer, MAT, "2026-11-09", "2027-05-09", event_id=created["id"]), 201)
    assert "ELIGIBILITY_VERIFY" in leave["flags"] and leave["status_v2"] == "PENDING"
    assert any(i["code"] == "ELIGIBILITY_VERIFY" for i in leave["issues"])


def test_lh_evt_08_paternity_working_days_window_and_intermittent_draws(acme):
    """Paternity (EVENT, 5 working days within 90 days of the birth): several requests may draw on one event."""
    worker, peer, boss = acme["worker"], acme["peer"], acme["boss"]
    live_with_events(acme)
    birth = ok(event(peer, leave_type=PAT, event_type="BIRTH", event_date="2026-10-12"), 201)
    assert (birth["entitled"], birth["take_until"]) == (5, "2027-01-10")
    late = apply(peer, PAT, "2027-01-08", "2027-01-11", event_id=birth["id"])
    assert late.json()["code"] == "EVENT_WINDOW_CLOSED" and late.json()["details"]["reason"] == "TOO_LATE"
    # Somebody else's event cannot be used.
    other = apply(worker, PAT, "2026-11-02", event_id=birth["id"])
    assert other.json()["code"] == "EVENT_REQUIRED"
    first = ok(apply(peer, PAT, "2026-10-30", "2026-11-02", event_id=birth["id"]), 201)  # Fri + Mon
    assert first["number_of_days"] == 2
    ok(boss.post(f"{L}/requests/{first['id']}/approve"))
    second = ok(apply(peer, PAT, "2026-11-04", "2026-11-05", event_id=birth["id"]), 201)
    assert second["number_of_days"] == 2
    card = balance(peer, PAT)
    assert (card["used"], card["pending"], card["available"]) == (2, 2, 1)
    over = apply(peer, PAT, "2026-11-09", "2026-11-10", event_id=birth["id"])
    assert over.json()["code"] == "INSUFFICIENT_BALANCE" and over.json()["details"]["available"] == 1
    ok(peer.post(f"{L}/requests/{second['id']}/withdraw"))
    assert balance(peer, PAT)["available"] == 3
    assert used_units(birth["id"]) == 2


def test_lh_sec_01_event_routes_are_scoped_and_tenant_isolated(app, acme):
    worker, peer, hr = acme["worker"], acme["peer"], acme["hr"]
    live_with_events(acme)
    created = ok(event(worker), 201)
    other = signup_company(app, "Other Event Co")
    foreign = invite_and_activate(app, other, create_employee(other, "Omar", "Haddad"), ["hr_admin"])
    assert foreign.get(E, params={"scope": "team"}).status_code == 404  # LEGACY_V1 tenant: no event routes
    backdate_settings(other.company_id)
    go_live({"cid": other.company_id, "hr": foreign})
    ghost = foreign.post(f"{E}/999999/cancel")
    stolen = foreign.post(f"{E}/{created['id']}/cancel")
    assert stolen.status_code == ghost.status_code == 404 and stolen.json() == ghost.json()
    assert ok(foreign.get(E, params={"scope": "team"})) == []
    assert peer.post(f"{E}/{created['id']}/cancel").status_code == 404  # a colleague outside her leave:read scope
    assert ok(peer.get(E, params={"scope": "team"})) == []
    assert worker.post(E, json={"leave_type_code": MAT, "event_type": "EXPECTED_DELIVERY",
                                "event_date": "2027-01-04", "company_id": 1}).status_code == 422  # strict body
    team = ok(hr.get(E, params={"scope": "team"}))
    assert [e["id"] for e in team] == [created["id"]] and team[0]["employee_id"] == acme["worker_id"]
    # HR records an event on behalf of an employee in scope.
    on_behalf = ok(hr.post(E, json={"leave_type_code": MAT, "event_type": "BIRTH", "event_date": "2026-10-10",
                                    "employee_id": acme["peer_id"]}), 201)
    assert on_behalf["employee_id"] == acme["peer_id"]
    assert ok(hr.post(f"{E}/{on_behalf['id']}/cancel"))["status"] == "CANCELLED"
    assert hr.post(f"{E}/{on_behalf['id']}/cancel").json()["code"] == "INVALID_TRANSITION"


def test_lh_mig_legacy_tenant_has_no_event_routes(acme):
    worker = acme["worker"]
    missing = event(worker)
    assert missing.status_code == 404
    assert worker.get(E).status_code == 404
