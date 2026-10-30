"""AS-M6 catalogue cases (12-edge-cases, AS-SWP-01..12, AS-REL-10; 03-shifts/03 §4, §6, §9, §16 GV-AS-SWP-15..21).

The plant week of ``test_shift_requests`` lies two weeks ahead, so cutoffs and expiries are reached by moving the
service clock (``shift_requests._now``); per-employee policy differences and leave facts are injected at the
service seams (``scheduling``, ``_on_leave``) that the request checks read.
"""

import threading
from datetime import date, datetime, timedelta

import pytest

from app.services import shift_requests as service
from tests.attendance.capture_v2_helpers import rows, sql
from tests.attendance_helpers import employee_with_login
from tests.helpers import login
from tests.shifts.test_shift_requests import entry, plant, submit  # noqa: F401


def _at(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(service, "_now", lambda: moment)


def _policy_override(monkeypatch, change):
    """Wrap ``scheduling`` so ``change(policy, employee_id)`` can edit the resolved SCHEDULING_POLICY."""
    real = service.scheduling

    def patched(db, company_id, employee_id, on):
        policy = real(db, company_id, employee_id, on)
        policy = {**policy, "requests": {k: (dict(v) if isinstance(v, dict) else v)
                                         for k, v in (policy.get("requests") or {}).items()}}
        change(policy, employee_id)
        return policy

    monkeypatch.setattr(service, "scheduling", patched)


def _cutoff(plant, who, body) -> datetime:
    r = plant["people"][who].post("/api/v1/shift-requests/preview", json=body)
    assert r.status_code == 200, r.text
    return datetime.fromisoformat(r.json()["cutoff_at"])


def test_as_swp_01_cutoff_boundary_to_the_second_against_the_earliest_affected_instance(plant, monkeypatch):
    fri = entry(plant, "Ravi", 4)
    body = {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]}
    cutoff = _cutoff(plant, "Ravi", body)
    _at(monkeypatch, cutoff + timedelta(seconds=1))
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json=body)
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_REQUEST_CUTOFF", r.text
    assert datetime.fromisoformat(r.json()["details"]["cutoff_at"]) == cutoff
    _at(monkeypatch, cutoff)  # equality is still in time
    req = submit(plant, "Ravi", body)
    assert datetime.fromisoformat(req["expires_at"]) == cutoff  # expires_at is the same instant
    # Cross-day: a counter entry on an earlier day moves the anchor to that day's instance.
    monkeypatch.undo()
    assert plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{req['id']}/withdraw").status_code == 200
    wed_sita = entry(plant, "Sita", 2)
    cross = {**body, "target_employee_id": plant["ids"]["Sita"], "counter_entry_id": wed_sita["id"]}
    assert _cutoff(plant, "Ravi", cross) == cutoff - timedelta(days=2)


def test_as_swp_02_expiry_is_evaluated_at_the_call_and_an_approver_may_extend_once(plant, monkeypatch):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    cutoff = datetime.fromisoformat(req["expires_at"])
    start = cutoff + timedelta(hours=24)  # swap cutoff 24 h before the earliest instance
    admin = plant["admin"]
    # Default approver_may_extend = false: refused.
    r = admin.post(f"/api/v1/shift-requests/{req['id']}/extend",
                   json={"expires_at": (cutoff + timedelta(hours=2)).isoformat(), "reason": "Waiting for Mohan"})
    assert r.status_code == 422 and r.json()["code"] == "FEATURE_DISABLED", r.text
    _policy_override(monkeypatch, lambda p, e: p["requests"]["swap"].update(approver_may_extend=True))
    r = admin.post(f"/api/v1/shift-requests/{req['id']}/extend",
                   json={"expires_at": (start + timedelta(minutes=1)).isoformat(), "reason": "Too late"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR", r.text  # never past the instance start
    # The requester and the coworker cannot extend their own request.
    r = plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/extend",
                                      json={"expires_at": (cutoff + timedelta(hours=2)).isoformat(), "reason": "Mine too"})
    assert r.status_code in (403, 404)
    new = cutoff + timedelta(hours=20)
    r = admin.post(f"/api/v1/shift-requests/{req['id']}/extend", json={"expires_at": new.isoformat(),
                                                                      "reason": "Mohan is travelling"})
    assert r.status_code == 200 and datetime.fromisoformat(r.json()["expires_at"]) == new, r.text
    audit = rows("SELECT details FROM audit_logs WHERE action = 'SHIFT_REQUEST_EXTENDED' AND entity_id = :i",
                 i=str(req["id"]))
    assert len(audit) == 1 and audit[0]["details"]["new_expires_at"] == new.isoformat()
    r = admin.post(f"/api/v1/shift-requests/{req['id']}/extend",
                   json={"expires_at": (new + timedelta(hours=1)).isoformat(), "reason": "Again"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"  # once only
    # After the old cutoff but before the extension the coworker can still accept.
    _at(monkeypatch, cutoff + timedelta(hours=1))
    r = plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={})
    assert r.status_code == 200 and r.json()["status"] == "PENDING_APPROVAL", r.text
    # Past the extended expiry the approval itself expires the request (the hourly job has not run).
    _at(monkeypatch, new + timedelta(seconds=1))
    r = admin.post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 409 and r.json()["details"]["current_status"] == "EXPIRED", r.text
    assert rows("SELECT status FROM shift_requests WHERE id = :i", i=req["id"])[0]["status"] == "EXPIRED"


def test_as_swp_03_a_kind_disabled_for_only_the_coworker_is_refused_naming_the_coworker(plant, monkeypatch):
    sita = plant["ids"]["Sita"]
    _policy_override(monkeypatch, lambda p, e: p["requests"]["swap"].update(enabled=False) if e == sita else None)
    fri = entry(plant, "Ravi", 4)
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={"kind": "SWAP", "entry_id": fri["id"],
                                                                     "target_employee_id": sita})
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_KIND_DISABLED", r.text
    assert r.json()["details"]["employee_id"] == sita
    assert rows("SELECT count(*) AS n FROM shift_requests")[0]["n"] == 0


def _saturday_open_shift(plant, approval: bool) -> dict:
    sat = plant["start"] + timedelta(days=5)
    r = plant["admin"].post("/api/v1/open-shifts", json={"branch_id": plant["branch"]["id"], "shift_date": sat.isoformat(),
                                                         "shift_id": plant["shifts"]["EARLY"], "required_count": 1,
                                                         "approval_required": approval})
    assert r.status_code == 201, r.text
    return r.json()


def test_as_swp_04_pickups_racing_for_the_last_place(plant):
    shift = _saturday_open_shift(plant, approval=True)
    a = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    b = plant["people"]["Sita"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert a.status_code == 201 and b.status_code == 201, (a.text, b.text)
    r = plant["admin"].post(f"/api/v1/shift-requests/{a.json()['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text
    assert rows("SELECT status FROM open_shifts WHERE id = :i", i=shift["id"])[0]["status"] == "FILLED"
    assert rows("SELECT status FROM shift_requests WHERE id = :i", i=b.json()["id"])[0]["status"] == "SUPERSEDED"
    late = plant["admin"].post(f"/api/v1/shift-requests/{b.json()['id']}/approve", json={})
    assert late.status_code == 409 and late.json()["details"]["current_status"] == "SUPERSEDED"
    # Without approval two simultaneous claims fill the single place exactly once.
    shift = _saturday_open_shift(plant, approval=False)
    results = {}

    def claim(name):
        results[name] = plant["people"][name].post(f"/api/v1/open-shifts/{shift['id']}/claim",
                                                   json={"acknowledged_warnings": ["UNAVAILABLE"]})

    threads = [threading.Thread(target=claim, args=(n,)) for n in ("Ravi", "Mohan")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    codes = sorted(r.status_code for r in results.values())
    assert codes in ([201, 409], [201, 422]), {n: r.text for n, r in results.items()}  # 422 only for rule refusals
    state = rows("SELECT status, filled_count FROM open_shifts WHERE id = :i", i=shift["id"])[0]
    assert state["filled_count"] <= 1


def test_as_swp_05_releaser_on_leave_before_the_pickup_applies(plant, monkeypatch):
    ravi = plant["ids"]["Ravi"]
    wed, thu = entry(plant, "Ravi", 2), entry(plant, "Ravi", 3)
    # (a) Leave applied while the released shift is unfilled: the open shift is cancelled, its claim superseded.
    submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": wed["id"], "reason": "Doctor"})
    wed_shift = rows("SELECT id FROM open_shifts WHERE released_by_employee_id = :e AND shift_date = :d",
                     e=ravi, d=wed["shift_date"])[0]["id"]
    claim = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{wed_shift}/claim", json={})
    assert claim.status_code == 201, claim.text
    from app.database.db import SessionLocal, bind_tenant

    with SessionLocal() as db:
        bind_tenant(db, plant["cid"])
        assert service.cancel_for_leave(db, plant["cid"], ravi, wed["shift_date"], wed["shift_date"]) == 1
        db.commit()
    assert rows("SELECT status FROM open_shifts WHERE id = :i", i=wed_shift)[0]["status"] == "CANCELLED"
    assert rows("SELECT status FROM shift_requests WHERE id = :i", i=claim.json()["id"])[0]["status"] == "SUPERSEDED"
    assert entry(plant, "Ravi", 2)["kind"] == "WORK"  # no silent refund: the entry stays
    # (b) Race: the leave is found at pickup apply -> NOT_ELIGIBLE OWN_LEAVE_OF_GIVER, entries unchanged.
    submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": thu["id"], "reason": "Doctor"})
    thu_shift = rows("SELECT id FROM open_shifts WHERE released_by_employee_id = :e AND shift_date = :d",
                     e=ravi, d=thu["shift_date"])[0]["id"]
    claim = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{thu_shift}/claim", json={})
    assert claim.status_code == 201, claim.text
    real = service._on_leave
    monkeypatch.setattr(service, "_on_leave", lambda db, c, e, d: e == ravi or real(db, c, e, d))
    r = plant["admin"].post(f"/api/v1/shift-requests/{claim.json()['id']}/approve", json={})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["OWN_LEAVE_OF_GIVER"], r.text
    assert entry(plant, "Ravi", 3)["kind"] == "WORK" and entry(plant, "Mohan", 3)["kind"] == "OFF"
    assert rows("SELECT status, close_reason FROM shift_requests WHERE id = :i", i=claim.json()["id"])[0] == {
        "status": "SUPERSEDED", "close_reason": "OWN_LEAVE_OF_GIVER"}  # kept although the call answered 422


def test_as_swp_05_a_pickup_approved_in_the_engine_inbox_while_the_giver_is_on_leave_is_superseded(plant, monkeypatch):
    """FU-AS-51 (c), EC AS-SWP-05 race in the engine inbox: the approver decides the claim from /approvals; the
    releaser is on leave by then. Raising would roll back the engine decision and lose the outcome, so the claim is
    SUPERSEDED (OWN_LEAVE_OF_GIVER) in the same transaction, the entries stay as published and the claimant is told."""
    ravi, mohan = plant["ids"]["Ravi"], plant["ids"]["Mohan"]
    thu = entry(plant, "Ravi", 3)
    submit(plant, "Ravi", {"kind": "RELEASE", "entry_id": thu["id"], "reason": "Doctor"})
    shift = rows("SELECT id FROM open_shifts WHERE released_by_employee_id = :e AND shift_date = :d",
                 e=ravi, d=thu["shift_date"])[0]["id"]
    claim = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift}/claim", json={})
    assert claim.status_code == 201, claim.text
    engine_id = rows("SELECT approval_request_id FROM shift_requests WHERE id = :i", i=claim.json()["id"])[0][
        "approval_request_id"]
    assert engine_id, "the claim is routed through the approval engine"
    real = service._on_leave
    monkeypatch.setattr(service, "_on_leave", lambda db, c, e, d: e == ravi or real(db, c, e, d))
    r = plant["admin"].post(f"/api/v1/payroll/approvals/{engine_id}/approve", json={})
    assert r.status_code == 200, r.text
    assert rows("SELECT status, close_reason FROM shift_requests WHERE id = :i", i=claim.json()["id"])[0] == {
        "status": "SUPERSEDED", "close_reason": "OWN_LEAVE_OF_GIVER"}
    assert entry(plant, "Ravi", 3)["kind"] == "WORK" and entry(plant, "Mohan", 3)["kind"] == "OFF"
    told = rows("SELECT count(*) AS n FROM notifications n JOIN employees e ON e.user_id = n.user_id "
                "WHERE e.id = :e AND n.title = 'Pickup not applied'", e=mohan)[0]["n"]
    assert told == 1


def test_as_swp_06_the_requester_never_sees_the_coworkers_reasons(plant, monkeypatch):
    sita = plant["ids"]["Sita"]
    real = service._on_leave
    monkeypatch.setattr(service, "_on_leave", lambda db, c, e, d: e == sita or real(db, c, e, d))
    fri = entry(plant, "Ravi", 4)
    picker = plant["people"]["Ravi"].post("/api/v1/shift-requests/preview",
                                          json={"kind": "SWAP", "entry_id": fri["id"]}).json()["coworkers"]
    row = next(c for c in picker if c["employee_id"] == sita)
    assert row["eligible"] is False and row["note"] == "Not available for this swap" and "reasons" not in row
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={"kind": "SWAP", "entry_id": fri["id"],
                                                                     "target_employee_id": sita})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["PEER_NOT_AVAILABLE"], r.text
    assert "LEAVE" not in r.text


def test_as_swp_07_a_period_paid_giver_cannot_drop_or_release_without_an_explicit_pay_effect(plant, monkeypatch):
    def unset(policy, _):
        for kind in ("drop", "release"):
            policy["requests"][kind]["pay_effect"] = None

    _policy_override(monkeypatch, unset)
    tue = entry(plant, "Ravi", 1)
    for body in ({"kind": "DROP", "entry_id": tue["id"], "target_employee_id": plant["ids"]["Mohan"]},
                 {"kind": "RELEASE", "entry_id": tue["id"]}):
        r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json=body)
        assert r.status_code == 422 and r.json()["details"]["reasons"] == ["GIVER_SALARIED"], r.text
    assert entry(plant, "Ravi", 1)["kind"] == "WORK"


def _compensation(plant, employee_id, template_type):
    """A minimal compensation assignment (ACTIVE from 2024) on a template of ``template_type``."""
    cid = plant["cid"]
    template = sql("INSERT INTO salary_templates (company_id, code, name, type, status, created_at, updated_at) "
                   "VALUES (:c, :code, :code, :t, 'ACTIVE', now(), now()) RETURNING id",
                   c=cid, code=f"T-{template_type}-{employee_id}", t=template_type).scalar()
    version = sql("INSERT INTO salary_template_versions (company_id, template_id, version, status, eligibility, "
                  "test_vectors, row_version, created_at, updated_at) VALUES (:c, :t, 1, 'DRAFT', '{}'::jsonb, "
                  "'[]'::jsonb, 1, now(), now()) RETURNING id", c=cid, t=template).scalar()
    placement = sql("SELECT id FROM employee_assignments WHERE employee_id = :e AND is_primary ORDER BY id DESC LIMIT 1",
                    e=employee_id).scalar()
    sql("INSERT INTO compensation_assignments (company_id, employee_id, assignment_id, template_version_id, "
        "pay_basis_amount, currency, effective_from, reason, status, attachments, split_period, row_version, "
        "created_at, updated_at) VALUES (:c, :e, :a, :v, 100, 'INR', '2024-01-01', 'HIRE', 'ACTIVE', '[]'::jsonb, "
        "false, 1, now(), now())", c=cid, e=employee_id, a=placement, v=version)


def test_as_swp_07_the_givers_pay_class_decides_the_default(plant, monkeypatch):
    """JC-AS-49 (owner decision IG-AS-150): without an explicit pay_effect, an hourly or daily giver may DROP (the
    vacated hours are simply unpaid) and a period-paid giver is refused GIVER_SALARIED."""
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import compensation

    def unset(policy, _):
        for kind in ("drop", "release"):
            policy["requests"][kind]["pay_effect"] = None

    _policy_override(monkeypatch, unset)
    _compensation(plant, plant["ids"]["Ravi"], "HOURLY")
    _compensation(plant, plant["ids"]["Sita"], "CTC_BASED")
    with SessionLocal() as db:
        bind_tenant(db, plant["cid"])
        day = plant["start"]
        assert compensation.pay_basis_class_on(db, plant["cid"], plant["ids"]["Ravi"], day) == "HOURLY_DAILY"
        assert compensation.pay_basis_class_on(db, plant["cid"], plant["ids"]["Sita"], day) == "PERIOD"
        assert compensation.pay_basis_class_on(db, plant["cid"], plant["ids"]["Mohan"], day) == "NONE"
    tue = entry(plant, "Ravi", 1)
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "DROP", "entry_id": tue["id"], "target_employee_id": plant["ids"]["Mohan"], "reason": "Exam"})
    assert r.status_code == 201, r.text  # hourly: allowed by default
    wed = entry(plant, "Sita", 2)
    r = plant["people"]["Sita"].post("/api/v1/shift-requests", json={
        "kind": "DROP", "entry_id": wed["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["GIVER_SALARIED"], r.text


def test_as_swp_08_a_priority_window_longer_than_the_time_left_is_shortened(plant, monkeypatch):
    probe = _saturday_open_shift(plant, approval=False)
    start = datetime.fromisoformat(probe["expires_at"])
    assert plant["admin"].post(f"/api/v1/open-shifts/{probe['id']}/cancel").status_code == 200
    posted_at = start - timedelta(hours=30)
    _at(monkeypatch, posted_at)
    sat = plant["start"] + timedelta(days=5)
    r = plant["admin"].post("/api/v1/open-shifts", json={"branch_id": plant["branch"]["id"], "shift_date": sat.isoformat(),
                                                         "shift_id": plant["shifts"]["EARLY"], "required_count": 2,
                                                         "approval_required": False, "exclusive_window_hours": 72})
    assert r.status_code == 201, r.text
    shift = r.json()
    until = datetime.fromisoformat(shift["priority_until"])
    assert until == posted_at + timedelta(hours=15) < start  # min(72 h, half the time left, start - 2 h)
    assert [w["code"] for w in rows("SELECT warnings FROM open_shifts WHERE id = :i", i=shift["id"])[0]["warnings"]] \
        == ["PRIORITY_WINDOW_SHORTENED"]
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 422 and "PRIORITY_WINDOW" in r.json()["details"]["reasons"], r.text
    # §6.4 EXISTING_PART_TIME_FIRST: a part-time employee (placement FTE < 1) is in the priority group.
    sql("UPDATE employee_assignments SET fte = 0.5 WHERE employee_id = :e", e=plant["ids"]["Sita"])
    r = plant["people"]["Sita"].post(f"/api/v1/open-shifts/{shift['id']}/claim",
                                     json={"acknowledged_warnings": ["UNAVAILABLE"]})
    assert r.status_code == 201, r.text
    _at(monkeypatch, until + timedelta(hours=1))
    r = plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={})
    assert r.status_code == 201, r.text


def test_the_approver_sees_each_claimants_ranking_inputs(plant):
    """§6.5 (D-AS-68, no automatic choice): per claim, scheduled hours in the claimant's week before → after the
    pickup, the worst check severity, skill tags and the priority-group flag; claims stay ordered by claim time."""
    sql("UPDATE employee_assignments SET fte = 0.5 WHERE employee_id = :e", e=plant["ids"]["Sita"])
    shift = _saturday_open_shift(plant, approval=True)
    assert plant["people"]["Mohan"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={}).status_code == 201
    assert plant["people"]["Sita"].post(f"/api/v1/open-shifts/{shift['id']}/claim", json={}).status_code == 201
    detail = plant["admin"].get(f"/api/v1/open-shifts/{shift['id']}").json()
    assert detail["scheduled_minutes"] == 480  # EARLY 06:00–14:00, no breaks
    mohan, sita = detail["claims"]  # claim order
    assert mohan["requester_employee_id"] == plant["ids"]["Mohan"]
    assert mohan["ranking"]["week_hours_before"] == 0 and mohan["ranking"]["week_hours_after"] == 8
    assert mohan["ranking"]["in_priority_group"] is False and sita["ranking"]["in_priority_group"] is True
    assert sita["ranking"]["week_hours_after"] - sita["ranking"]["week_hours_before"] == 8
    assert sita["ranking"]["week_hours_before"] > 0  # Sita already works this week
    assert {mohan["ranking"]["worst_severity"], sita["ranking"]["worst_severity"]} <= set(
        ("PASS", "INFO", "WARN", "REQUIRE_REASON", "BLOCK"))


def test_as_swp_09_an_approver_covering_only_one_party_never_sees_or_decides_the_request(app, plant):
    admin = plant["admin"]
    role = admin.post("/api/v1/roles", json={"name": "Line lead", "description": "approves direct reports", "grants": [
        {"permission": "roster:read", "scope": "DIRECT_REPORTS"}, {"permission": "roster:approve", "scope": "DIRECT_REPORTS"}]})
    assert role.status_code in (200, 201), role.text
    lead_client = employee_with_login(app, admin, "Vikram", branch_id=plant["branch"]["id"])
    lead_id = lead_client.me()["employee"]["id"]
    assert admin.put(f"/api/v1/users/{lead_client.user_id}/roles", json={"role_ids": [role.json()["id"]]}).status_code == 200
    r = admin.put(f"/api/v1/org/employees/{plant['ids']['Ravi']}/manager", json={"manager_id": lead_id})
    assert r.status_code == 200, r.text
    lead = login(app, lead_client.email)
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={}).status_code == 200
    inbox = lead.get("/api/v1/shift-requests", params={"box": "approvals"}).json()["items"]
    assert req["id"] not in [i["id"] for i in inbox]  # Vikram covers Ravi, not Mohan
    assert lead.get(f"/api/v1/shift-requests/{req['id']}").status_code == 404
    assert lead.post(f"/api/v1/shift-requests/{req['id']}/approve", json={}).status_code == 404
    assert rows("SELECT status FROM shift_requests WHERE id = :i", i=req["id"])[0]["status"] == "PENDING_APPROVAL"
    # Engine half (owner decision IG-AS-150): the engine request names both parties and never offers it to Vikram.
    engine_id = rows("SELECT approval_request_id FROM shift_requests WHERE id = :i", i=req["id"])[0][
        "approval_request_id"]
    context = rows("SELECT context FROM approval_requests WHERE id = :i", i=engine_id)[0]["context"]
    assert sorted(context["party_employee_ids"]) == sorted([plant["ids"]["Ravi"], plant["ids"]["Mohan"]])
    engine_inbox = lead.get("/api/v1/approvals", params={"module": "shift_request"}).json().get("items", [])
    assert engine_id not in [i["id"] for i in engine_inbox]
    r = lead.post(f"/api/v1/approvals/{engine_id}/decide", json={"decision": "APPROVE"})
    assert r.status_code in (403, 404), r.text
    # Once Mohan reports to Vikram too, both parties are covered and the request reaches his inbox.
    assert admin.put(f"/api/v1/org/employees/{plant['ids']['Mohan']}/manager", json={"manager_id": lead_id}).status_code == 200
    inbox = lead.get("/api/v1/shift-requests", params={"box": "approvals"}).json()["items"]
    assert req["id"] in [i["id"] for i in inbox]
    assert lead.get(f"/api/v1/shift-requests/{req['id']}").status_code == 200


def test_as_swp_10_requests_per_period_are_limited_and_withdrawn_ones_do_not_count(plant, monkeypatch):
    _policy_override(monkeypatch, lambda p, e: p["requests"]["swap"].update(max_per_period=1, limit_window="MONTH"))
    fri, mon = entry(plant, "Ravi", 4), entry(plant, "Ravi", 0)
    first = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{first['id']}/withdraw").status_code == 200
    kept = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert kept["status"] == "PENDING_ACCEPTANCE"
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={"kind": "SWAP", "entry_id": mon["id"],
                                                                     "target_employee_id": plant["ids"]["Mohan"]})
    if fri["shift_date"].month != mon["shift_date"].month:
        pytest.skip("the plant week straddles two months")
    assert r.status_code == 422 and r.json()["code"] == "REQUEST_LIMIT_REACHED", r.text
    assert {k: r.json()["details"][k] for k in ("kind", "used", "limit")} == {"kind": "SWAP", "used": 1, "limit": 1}


def _supersede_friday(plant):
    """Ravi's Friday swap with Mohan, accepted, then an EMPLOYER publication moves Ravi's Friday to LATE."""
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={}).status_code == 200
    cell = {"action": "UPSERT", "employee_id": plant["ids"]["Ravi"], "shift_date": fri["shift_date"].isoformat(),
            "entry_seq": 1, "kind": "WORK", "shift_id": plant["shifts"]["LATE"]}
    dry = plant["admin"].post("/api/v1/rosters/direct-publish", json={"cells": [cell], "reason_code": "PLANNER",
                                                                       "dry_run": True}).json()
    warns = sorted({x["rule_key"] for x in dry["results"] if x["severity"] == "WARN"})
    reasons = {x["rule_key"]: "Cover needed" for x in dry["results"] if x["severity"] == "REQUIRE_REASON"}
    r = plant["admin"].post("/api/v1/rosters/direct-publish", json={"cells": [cell], "reason_code": "PLANNER",
                                                                     "acknowledged_warnings": warns, "reasons": reasons})
    assert r.status_code == 200, r.text
    assert rows("SELECT status, close_reason FROM shift_requests WHERE id = :i", i=req["id"])[0] == {
        "status": "SUPERSEDED", "close_reason": "SCHEDULE_CHANGED"}
    return fri, req


def test_as_swp_11_a_request_superseded_by_the_employer_late_can_be_refiled_once(plant, monkeypatch):
    fri, req = _supersede_friday(plant)
    body = {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]}
    start = _cutoff(plant, "Ravi", body) + timedelta(hours=24)  # the LATE instance start
    superseded_at = start - timedelta(hours=30)  # inside the 48 h before the instance
    sql("UPDATE shift_requests SET updated_at = :t WHERE id = :i", t=superseded_at, i=req["id"])
    refile = {**body, "refile_of_request_id": req["id"], "reason": "Agreed with Mohan"}
    # Past the normal cutoff (start - 24 h) a plain filing is refused ...
    _at(monkeypatch, start - timedelta(hours=20))
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json=body)
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_REQUEST_CUTOFF"
    # ... but the parties must stay the same for a re-file ...
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={**refile, "target_employee_id": plant["ids"]["Sita"]})
    assert r.status_code == 400 and r.json()["code"] == "VALIDATION_ERROR", r.text
    # ... and a re-file within 24 h of the supersede is accepted, bounded by the instance start.
    again = submit(plant, "Ravi", refile)
    assert again["status"] == "PENDING_ACCEPTANCE" and datetime.fromisoformat(again["expires_at"]) == start
    assert rows("SELECT refile_of_request_id FROM shift_requests WHERE id = :i", i=again["id"])[0][
        "refile_of_request_id"] == req["id"]
    # A second re-file of the same chain is refused.
    assert plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{again['id']}/withdraw").status_code == 200
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json=refile)
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_REQUEST_CUTOFF", r.text
    # More than 24 h after the supersede the window is closed.
    sql("DELETE FROM shift_requests WHERE id = :i", i=again["id"])
    _at(monkeypatch, superseded_at + timedelta(hours=25))
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json=refile)
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_REQUEST_CUTOFF", r.text


def test_as_swp_12_a_withdrawn_request_cannot_be_accepted_or_approved(plant):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert plant["people"]["Ravi"].post(f"/api/v1/shift-requests/{req['id']}/withdraw").status_code == 200
    r = plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={})
    assert r.status_code == 409 and r.json()["details"]["current_status"] == "CANCELLED", r.text
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 409 and r.json()["details"]["current_status"] == "CANCELLED", r.text
    assert entry(plant, "Ravi", 4)["version"] == 1 and entry(plant, "Mohan", 4)["version"] == 1


def test_as_swp_12_a_the_coworkers_side_is_checked_again_at_approval(plant, monkeypatch):
    """(a) Mohan is transferred (or goes on leave) between his acceptance and the approval: 422 NOT_ELIGIBLE with
    the reason shown to the approver; nothing is written and the request stays PENDING_APPROVAL for a reject."""
    mohan = plant["ids"]["Mohan"]
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": mohan})
    assert plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={}).status_code == 200
    real = service._branch_on
    monkeypatch.setattr(service, "_branch_on", lambda db, e, d: -1 if e is not None and e.id == mohan else real(db, e, d))
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["BRANCH"], r.text
    monkeypatch.setattr(service, "_branch_on", real)
    leave = service._on_leave
    monkeypatch.setattr(service, "_on_leave", lambda db, c, e, d: e == mohan or leave(db, c, e, d))
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["LEAVE"], r.text
    assert rows("SELECT status FROM shift_requests WHERE id = :i", i=req["id"])[0]["status"] == "PENDING_APPROVAL"
    assert entry(plant, "Ravi", 4)["version"] == 1 and entry(plant, "Mohan", 4)["version"] == 1
    monkeypatch.setattr(service, "_on_leave", leave)
    r = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED", r.text


def test_as_swp_12_b_an_unpublished_day_is_named_for_the_requester_and_masked_for_the_coworker(plant):
    fri = entry(plant, "Ravi", 4)
    sat_sita, fri_mohan = entry(plant, "Sita", 5), entry(plant, "Mohan", 4)
    sql("UPDATE roster_entries SET withdrawn_at = now() WHERE employee_id = :e AND shift_date = :d",
        e=plant["ids"]["Ravi"], d=sat_sita["shift_date"])  # Ravi's Saturday is no longer published
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Sita"],
        "counter_entry_id": sat_sita["id"]})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["ROSTER_UNPUBLISHED"], r.text
    sql("UPDATE roster_entries SET withdrawn_at = now() WHERE id = :i", i=fri_mohan["id"])
    r = plant["people"]["Ravi"].post("/api/v1/shift-requests", json={
        "kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert r.status_code == 422 and r.json()["details"]["reasons"] == ["PEER_NOT_AVAILABLE"], r.text
    assert rows("SELECT count(*) AS n FROM shift_requests")[0]["n"] == 0


def test_a_swap_exchanges_every_entry_of_a_multi_shift_day():
    """§5.1 / FU-AS-47: the two cells exchange their whole content, entry by entry. A two-entry day swapped with a
    one-entry day: the first entries are updated, the surplus entry is withdrawn on one side and created (next
    entry_seq) on the other. (A real two-WORK-entry day is refused by the 11 h rest rule in the fixture, so the
    exchange plan is checked directly.)"""
    from types import SimpleNamespace

    def cell(seq, kind, shift_id=None):
        return SimpleNamespace(entry_seq=seq, kind=kind, shift_id=shift_id, custom_start_time=None,
                               custom_end_time=None, custom_segments=None, site_id=None, planned_work_mode=None)

    day = date(2026, 11, 13)
    two = [cell(1, "WORK", 11), cell(2, "WORK", 12)]
    one = [cell(1, "OFF")]
    to_one = service._exchange(1, day, two, [service._content(e) for e in one])
    assert [(c.entry_seq, c.kind) for c in to_one] == [(1, "OFF"), (2, None)]  # update + withdraw
    to_two = service._exchange(2, day, one, [service._content(e) for e in two])
    assert [(c.entry_seq, c.kind, c.shift_id) for c in to_two] == [(1, "WORK", 11), (2, "WORK", 12)]  # update + create
    assert service._exchange(3, day, one, [service._content(e) for e in one]) == []  # identical: nothing


def test_as_rel_10_swap_approval_and_a_publish_of_the_same_cell_at_once(plant):
    fri = entry(plant, "Ravi", 4)
    req = submit(plant, "Ravi", {"kind": "SWAP", "entry_id": fri["id"], "target_employee_id": plant["ids"]["Mohan"]})
    assert plant["people"]["Mohan"].post(f"/api/v1/shift-requests/{req['id']}/accept", json={}).status_code == 200
    cell = {"action": "UPSERT", "employee_id": plant["ids"]["Ravi"], "shift_date": fri["shift_date"].isoformat(),
            "entry_seq": 1, "kind": "OFF"}
    out = {}

    def approve():
        out["approve"] = plant["admin"].post(f"/api/v1/shift-requests/{req['id']}/approve", json={})

    def publish():
        out["publish"] = plant["admin"].post("/api/v1/rosters/direct-publish", json={
            "cells": [cell], "reason_code": "PLANNER", "acknowledged_warnings": ["LINT_EARLY_START"]})

    threads = [threading.Thread(target=approve), threading.Thread(target=publish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert all(r.status_code < 500 for r in out.values()), {k: r.text for k, r in out.items()}  # no deadlock / 500
    status = rows("SELECT status FROM shift_requests WHERE id = :i", i=req["id"])[0]["status"]
    assert status in ("APPROVED", "SUPERSEDED")  # the only two outcomes
    if status == "SUPERSEDED":
        assert out["publish"].status_code == 200
