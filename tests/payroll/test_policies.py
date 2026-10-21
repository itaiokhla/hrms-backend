"""Payroll policies: CRUD, versions, assignment rules, resolution, archive (3.1 §3.2, §9).

EC-CFG-05, EC-CFG-07, EC-CFG-16, EC-CFG-17, EC-CFG-18, EC-CFG-20.
"""

import importlib
from datetime import timedelta

import pytest

from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_settings_engine import (
    change,
    default_placement,
    make_branch,
    make_entity,
    next_month,
    place,
    put,
    session,
    sql,
    today_of,
)

POLICIES = "/api/v1/payroll/policies"
OT_FIELDS = {"buckets": [
    {"day_type": "WORKDAY", "after_hours": "0", "multiplier": "1.5"},
    {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": "2"},
    {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": "2"},
]}


def _by_type(client, **params):
    r = client.get(POLICIES, params=params)
    assert r.status_code == 200, r.text
    return {p["type"]: p for p in r.json()}


def _create(client, type_code, name, **extra):
    r = client.post(POLICIES, json={"type": type_code, "name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _resolve(client, type_code, employee_id, **params):
    r = client.get(f"{POLICIES}/resolve", params={"type": type_code, "employee_id": employee_id, **params})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def acme(app):
    client = signup_company(app, "Acme Corp")
    le1, br1 = default_placement(client.company_id)
    le2 = make_entity(client, "AE01")
    br2 = make_branch(client, le2, "DXB")
    worker = create_employee(client, "Worker")
    place(client.company_id, worker["id"], le2, br2)
    stayer = create_employee(client, "Stayer")
    client.org = {"le1": le1, "le2": le2, "br2": br2, "worker": worker["id"], "stayer": stayer["id"]}
    return client


def test_starter_policies_are_listed_with_their_versions_and_catch_all_rules(acme):
    policies = _by_type(acme)
    assert set(policies) == {p.type for p in pt.starter_policies("STARTER_V1", leave_types=[])}
    attendance = policies["ATTENDANCE_PAY_POLICY"]
    assert attendance["active_version"]["status"] == "ACTIVE" and attendance["active_version"]["policy_hash"]
    assert policies["OVERTIME_POLICY"]["active_version"] is None and policies["OVERTIME_POLICY"]["draft_versions"] == [1]
    assert list(_by_type(acme, type="OVERTIME_POLICY")) == ["OVERTIME_POLICY"]
    detail = acme.get(f"{POLICIES}/{attendance['id']}").json()
    assert [(r["criteria"], r["priority"]) for r in detail["assignments"]] == [({"everyone": True}, 0)]
    assert detail["versions"][0]["fields"]["unmarked_day"] == "EXCEPTION"
    assert acme.get(f"{POLICIES}/{attendance['id']}/assignments").json()["rules"][0]["priority"] == 0
    assert acme.get(POLICIES, params={"type": "NOPE"}).json()["code"] == "UNKNOWN_POLICY_TYPE"


def test_versions_are_drafted_edited_and_activated_closing_the_previous_one(acme):
    cid = acme.company_id
    policy = _create(acme, "OVERTIME_POLICY", "Factory OT", description="Plant workers")
    pid = policy["id"]
    assert policy["versions"][0]["status"] == "DRAFT" and policy["versions"][0]["fields"]["buckets"][0]["multiplier"] is None
    r = acme.post(f"{POLICIES}/{pid}/versions/1/activate")
    assert r.status_code == 400 and r.json()["code"] == "FIELD_REQUIRED_FOR_ACTIVATION"  # no multiplier is assumed
    r = acme.put(f"{POLICIES}/{pid}/versions/1", json={"fields": OT_FIELDS})
    assert r.status_code == 200 and r.json()["fields"]["buckets"][0]["multiplier"] == "1.5"
    r = acme.post(f"{POLICIES}/{pid}/versions/1/activate")
    assert r.status_code == 200, r.text
    v1 = r.json()
    assert v1["status"] == "ACTIVE" and v1["policy_hash"] == pt.policy_hash(v1["fields"])
    assert v1["message"].startswith("Applies from ")  # overtime is DAY grain
    assert acme.put(f"{POLICIES}/{pid}/versions/1", json={"fields": OT_FIELDS}).json()["code"] == "VERSION_NOT_DRAFT"
    assert acme.post(f"{POLICIES}/{pid}/versions/1/activate").json()["code"] == "VERSION_NOT_DRAFT"

    v2 = acme.post(f"{POLICIES}/{pid}/versions").json()
    assert v2["version"] == 2 and v2["status"] == "DRAFT" and v2["fields"] == v1["fields"]  # copied
    fields = {**OT_FIELDS, "buckets": [{**OT_FIELDS["buckets"][0], "multiplier": "1.75"}, *OT_FIELDS["buckets"][1:]]}
    assert acme.put(f"{POLICIES}/{pid}/versions/2", json={"fields": fields}).status_code == 200
    first, _, _ = next_month(cid, 1)
    r = acme.post(f"{POLICIES}/{pid}/versions/2/activate", json={"effective_from": first.isoformat()})
    assert r.status_code == 200, r.text
    versions = {v["version"]: v for v in acme.get(f"{POLICIES}/{pid}").json()["versions"]}
    assert versions[1]["effective_to"] == (first - timedelta(days=1)).isoformat() and versions[1]["status"] == "ACTIVE"
    assert versions[2]["effective_from"] == first.isoformat() and versions[2]["effective_to"] is None

    yesterday = today_of(cid) - timedelta(days=1)
    v3 = acme.post(f"{POLICIES}/{pid}/versions", json={"fields": OT_FIELDS, "effective_from": yesterday.isoformat()})
    assert v3.status_code == 201
    r = acme.post(f"{POLICIES}/{pid}/versions/3/activate")
    assert r.status_code == 400 and r.json()["code"] == "REASON_REQUIRED"
    r = acme.post(f"{POLICIES}/{pid}/versions/3/activate", json={"reason": "Correction agreed with the union"})
    assert r.status_code == 200, r.text
    versions = {v["version"]: v for v in acme.get(f"{POLICIES}/{pid}").json()["versions"]}
    assert versions[1]["status"] == "SUPERSEDED"  # started today, after the backdated version
    assert versions[3]["effective_to"] == (first - timedelta(days=1)).isoformat()  # the scheduled v2 stays
    assert versions[2]["status"] == "ACTIVE"
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Payroll Policies' "
                                 "AND entity_id = :p ORDER BY id", c=cid, p=str(pid))]
    assert actions[:4] == ["POLICY_CREATED", "POLICY_VERSION_UPDATED", "POLICY_VERSION_ACTIVATED", "POLICY_VERSION_CREATED"]


def test_ec_cfg_05_same_priority_rules_matching_one_employee_are_rejected(acme):
    org = acme.org
    factory = _create(acme, "ATTENDANCE_PAY_POLICY", "Factory attendance", fields={"unmarked_day": "ABSENT"})
    url = f"{POLICIES}/{factory['id']}/assignments"
    r = acme.put(url, json={"rules": [{"criteria": {"everyone": True}, "priority": 0}]})
    assert r.status_code == 400 and r.json()["code"] == "PRIORITY_CONFLICT"
    conflict = r.json()["detail"]["details"]["conflicts"][0]
    assert {org["worker"], org["stayer"]} <= set(conflict["employee_ids"])  # the clashing employees are listed
    r = acme.put(url, json={"rules": [{"criteria": {"employee_ids": [org["worker"]]}, "priority": 5},
                                      {"criteria": {"legal_entity_ids": [org["le2"]]}, "priority": 5}]})
    assert r.status_code == 400 and r.json()["detail"]["details"]["conflicts"][0]["employee_ids"] == [org["worker"]]
    r = acme.put(url, json={"rules": [{"criteria": {"legal_entity_ids": [org["le2"]]}, "priority": 10}]})
    assert r.status_code == 200, r.text

    pending = _resolve(acme, "ATTENDANCE_PAY_POLICY", org["worker"])
    assert pending["status"] == "NO_ACTIVE_VERSION" and pending["policy"]["name"] == "Factory attendance"
    assert acme.post(f"{POLICIES}/{factory['id']}/versions/1/activate").status_code == 200
    found = _resolve(acme, "ATTENDANCE_PAY_POLICY", org["worker"])
    assert found["status"] == "RESOLVED" and found["version"]["fields"]["unmarked_day"] == "ABSENT"
    assert found["rule"]["priority"] == 10 and "priority 10" in found["reason"] and "lower-priority" in found["reason"]
    other = _resolve(acme, "ATTENDANCE_PAY_POLICY", org["stayer"])
    assert other["status"] == "RESOLVED" and other["policy"]["name"] == "Default attendance pay"
    # Replacing the rules keeps the old ones as history.
    assert acme.put(url, json={"rules": [{"criteria": {"employee_ids": [org["stayer"]]}, "priority": 10}]}).status_code == 200
    detail = acme.get(f"{POLICIES}/{factory['id']}").json()
    assert len(detail["assignments"]) == 1 and detail["rule_history"][0]["status"] == "INACTIVE"
    assert _resolve(acme, "ATTENDANCE_PAY_POLICY", org["stayer"])["policy"]["name"] == "Factory attendance"


def test_ec_cfg_07_employees_without_policy_and_ec_cfg_16_archive_instead_of_delete(acme):
    from app.payroll.services import policies as service

    cid = acme.company_id
    attendance = _by_type(acme)["ATTENDANCE_PAY_POLICY"]
    r = acme.post(f"{POLICIES}/{attendance['id']}/archive")
    assert r.status_code == 200 and r.json() == {"id": attendance["id"], "status": "ARCHIVED", "used_by_runs": False}
    assert acme.post(f"{POLICIES}/{attendance['id']}/archive").json()["code"] == "POLICY_ARCHIVED"
    assert acme.delete(f"{POLICIES}/{attendance['id']}").status_code == 405  # never deleted
    assert "ATTENDANCE_PAY_POLICY" not in _by_type(acme)
    assert _by_type(acme, include_archived=True)["ATTENDANCE_PAY_POLICY"]["status"] == "ARCHIVED"
    assert _resolve(acme, "ATTENDANCE_PAY_POLICY", acme.org["worker"])["status"] == "NO_POLICY"
    db = session(cid)
    try:
        on = today_of(cid)
        missing = service.employees_without_policy(db, cid, "ATTENDANCE_PAY_POLICY", on=on)
        assert {acme.org["worker"], acme.org["stayer"]} <= {m["employee_id"] for m in missing}
        entity_only = service.employees_without_policy(db, cid, "ATTENDANCE_PAY_POLICY", on=on,
                                                       legal_entity_id=acme.org["le2"])
        assert entity_only == [{"employee_id": acme.org["worker"], "status": "NO_POLICY"}]
        # The starter overtime policy is a draft: with overtime ON every employee is a V-11 exception.
        drafts = service.employees_without_policy(db, cid, "OVERTIME_POLICY", on=on)
        assert {m["status"] for m in drafts} == {"NO_ACTIVE_VERSION"}
        assert service.employees_without_policy(db, cid, "PAYSLIP_TEMPLATE", on=on) == []
    finally:
        db.close()
    assert acme.post(f"{POLICIES}/{attendance['id']}/versions").json()["code"] == "POLICY_ARCHIVED"
    # Turning a switch ON with only draft policies is allowed: V-11 is a run pre-check, not a save error.
    r = put(acme, change("payroll.overtime.enabled", True, row_version=1))
    assert r.status_code == 200, r.text


def test_strict_payloads_and_references(acme):
    attendance = _by_type(acme)["ATTENDANCE_PAY_POLICY"]
    url = f"{POLICIES}/{attendance['id']}/assignments"
    r = acme.put(url, json={"rules": [{"criteria": {"employee_ids": [999999]}, "priority": 3}]})
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_REFERENCE"
    r = acme.put(url, json={"rules": [{"criteria": {"region": ["north"]}, "priority": 3}]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_CRITERIA"
    assert acme.put(url, json={"rules": [], "company_id": 1}).status_code == 422
    assert acme.post(POLICIES, json={"type": "OVERTIME_POLICY", "name": "X", "status": "ACTIVE"}).status_code == 422
    r = acme.post(POLICIES, json={"type": "OVERTIME_POLICY", "name": "Floaty", "fields": {"min_minutes": 1.5}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_FIELDS"
    assert acme.post(POLICIES, json={"type": "NOPE", "name": "X"}).json()["code"] == "UNKNOWN_POLICY_TYPE"
    r = acme.post(POLICIES, json={"type": "ATTENDANCE_PAY_POLICY", "name": "default ATTENDANCE pay"})
    assert r.status_code == 409 and r.json()["code"] == "POLICY_NAME_TAKEN"
    chain = _by_type(acme).get("APPROVAL_CHAIN")
    if chain is None:
        chain = _create(acme, "APPROVAL_CHAIN", "Payroll chain", fields={"module": "payroll_run"}) \
            if acme.post(POLICIES, json={"type": "APPROVAL_CHAIN", "name": "probe"}).status_code == 201 else None
    if chain is not None:
        r = acme.put(f"{POLICIES}/{chain['id']}/assignments", json={"rules": []})
        assert r.status_code == 400 and r.json()["code"] == "NOT_ASSIGNABLE"


def test_ec_cfg_20_activation_respects_country_pack_legal_limits(acme, monkeypatch):
    packs = importlib.import_module("app.payroll.packs")

    def setting_constraints(country_code, on):
        if country_code != "AE":
            return ()
        return (reg.LegalConstraint("OVERTIME_POLICY.buckets.multiplier", "Test Labour Law art. 3",
                                    minimum=reg.Decimal("2"), match={"day_type": "WORKDAY"}),)

    monkeypatch.setattr(packs, "setting_constraints", setting_constraints, raising=False)
    policy = _create(acme, "OVERTIME_POLICY", "Gulf OT", fields=OT_FIELDS)
    r = acme.post(f"{POLICIES}/{policy['id']}/versions/1/activate")
    assert r.status_code == 400 and r.json()["code"] == "LEGAL_LIMIT"
    assert r.json()["detail"]["issues"][0]["rule"] == "V-16" and "Test Labour Law art. 3" in r.json()["detail"]["message"]
    fields = {"buckets": [{**OT_FIELDS["buckets"][0], "multiplier": "2.5"}, *OT_FIELDS["buckets"][1:]]}
    assert acme.put(f"{POLICIES}/{policy['id']}/versions/1", json={"fields": fields}).status_code == 200
    assert acme.post(f"{POLICIES}/{policy['id']}/versions/1/activate").status_code == 200


def test_authorization_payroll_configuration_permissions(app, acme):
    employee = invite_and_activate(app, acme, create_employee(acme, "Plain"), ["employee"])
    processor = invite_and_activate(app, acme, create_employee(acme, "Proc"), ["payroll_processor"])
    pid = _by_type(acme)["OVERTIME_POLICY"]["id"]
    assert employee.get(POLICIES).status_code == 403
    assert employee.get(f"{POLICIES}/{pid}").status_code == 403
    assert employee.post(POLICIES, json={"type": "OVERTIME_POLICY", "name": "X"}).status_code == 403
    assert processor.get(POLICIES).status_code == 200 and processor.get(f"{POLICIES}/{pid}").status_code == 200
    for method, url, body in (("post", POLICIES, {"type": "OVERTIME_POLICY", "name": "X"}),
                              ("post", f"{POLICIES}/{pid}/versions", None),
                              ("put", f"{POLICIES}/{pid}/versions/1", {"fields": OT_FIELDS}),
                              ("post", f"{POLICIES}/{pid}/versions/1/activate", None),
                              ("put", f"{POLICIES}/{pid}/assignments", {"rules": []}),
                              ("post", f"{POLICIES}/{pid}/archive", None)):
        assert getattr(processor, method)(url, json=body).status_code == 403, url


def test_ec_cfg_17_three_tenants_cannot_reach_each_others_policies(app, acme):
    beta, cobalt = signup_company(app, "Beta Industries"), signup_company(app, "Cobalt Labs")
    pid = _by_type(acme)["OVERTIME_POLICY"]["id"]
    for other in (beta, cobalt):
        assert other.get(f"{POLICIES}/{pid}").status_code == 404
        assert other.get(f"{POLICIES}/{pid}/assignments").status_code == 404
        assert other.put(f"{POLICIES}/{pid}/assignments", json={"rules": []}).status_code == 404
        assert other.post(f"{POLICIES}/{pid}/versions").status_code == 404
        assert other.put(f"{POLICIES}/{pid}/versions/1", json={"fields": OT_FIELDS}).status_code == 404
        assert other.post(f"{POLICIES}/{pid}/versions/1/activate").status_code == 404
        assert other.post(f"{POLICIES}/{pid}/archive").status_code == 404
        r = other.get(f"{POLICIES}/resolve", params={"type": "OVERTIME_POLICY", "employee_id": acme.org["worker"]})
        assert r.status_code == 404
        assert pid not in {p["id"] for p in other.get(POLICIES).json()}
    r = beta.put(f"{POLICIES}/{_by_type(beta)['OVERTIME_POLICY']['id']}/assignments",
                 json={"rules": [{"criteria": {"employee_ids": [acme.org["worker"]]}, "priority": 3}]})
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_REFERENCE"  # another tenant's employee
    assert _by_type(acme)["OVERTIME_POLICY"]["rule_count"] == 1


def test_idempotency_key_replays_policy_creation(acme):
    headers = {"Idempotency-Key": "create-policy-1"}
    body = {"type": "SHIFT_ALLOWANCE_POLICY", "name": "Night shift"}
    first = acme.post(POLICIES, json=body, headers=headers)
    again = acme.post(POLICIES, json=body, headers=headers)
    assert first.status_code == again.status_code == 201
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json()["id"] == first.json()["id"]
    assert sql("SELECT count(*) FROM policies WHERE company_id = :c AND name = 'Night shift'",
               c=acme.company_id)[0][0] == 1
