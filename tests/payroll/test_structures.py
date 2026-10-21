"""Salary structures: versions, publish validation, preview, move to a new version (3.3 §6, 3.8 §5.8).

EC-CMP-01, EC-CMP-04, EC-CMP-07, EC-CMP-17.
"""

from decimal import Decimal

import pytest

from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_compensation import (
    approve,
    assign,
    assign_and_approve,
    employee_url,
    month_start,
    next_month,
    people,
)
from tests.payroll.test_components import (
    EXAMPLE_A,
    STRUCTURES,
    active_version_id,
    make_structure,
    ok,
    setup_catalogue,
)
from tests.payroll.test_settings_engine import sql

FLOORED = [{"component": "BASIC", "method": "FIXED", "value": "20000"},
           {"component": "HRA", "method": "PERCENT_OF", "value": "40", "params": {"bases": ["BASIC"]}},
           {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}]


def entities(client):
    return {e["code"]: e for e in ok(client.get("/api/v1/legal-entities"))}


def gulf_entity(client):
    r = client.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Gulf FZ-LLC", "country_code": "AE",
                                                    "base_currency": "AED", "timezone": "Asia/Dubai"})
    assert r.status_code == 200, r.text
    return r.json()


def preview(client, structure_id, amount, currency="INR", **extra):
    return client.post(f"{STRUCTURES}/{structure_id}/preview",
                       json={"pay_basis_amount": {"amount": amount, "currency": currency}, **extra})


@pytest.fixture
def acme(app):
    client = signup_company(app, "Acme Corp")
    setup_catalogue(client)
    return client


# --------------------------------------------------------------------------- create, publish, preview
def test_example_a_publish_and_preview_monthly_and_annual(acme):
    created = make_structure(acme, publish=False)
    assert created["draft_version"] == 1 and created["active_version"] is None
    lines = {line["component"]: line for line in created["versions"][0]["lines"]}
    assert lines["HRA"]["value"] == "40" and lines["HRA"]["method"] == "PERCENT_OF"
    assert lines["BASIC"]["formula"] == "ctc.monthly * 50%"
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={"version": 1}))
    assert report["ok"] is True and report["currency"] == "INR" and report["minimum_pay_basis"] is None
    published = ok(acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}))
    assert published["active_version"] == 1 and published["versions"][0]["validation"]["ok"] is True
    result = ok(preview(acme, created["id"], "1200000"))
    rows = {row["component"]: row for row in result["lines"]}
    assert rows["BASIC"]["monthly"] == {"amount": "50000.00", "currency": "INR"}
    assert rows["HRA"]["monthly"]["amount"] == "20000.00" and rows["HRA"]["annual"]["amount"] == "240000.00"
    assert rows["FIXED_ALLOWANCE"]["monthly"]["amount"] == "30000.00"  # no verified pack: no employer PF inside CTC
    assert result["totals"]["gross"]["monthly"]["amount"] == "100000.00"
    assert result["totals"]["ctc"]["annual"]["amount"] == "1200000.00"
    assert result["statutory"]["label"] == "Compliance not verified" and result["statutory"]["verified"] is False
    assert result["pay_basis_amount"] == {"amount": "1200000.00", "currency": "INR"}
    assert preview(acme, created["id"], 1200000).status_code == 422  # never a JSON number
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND entity_type = "
                                 "'salary_structure' ORDER BY id", c=acme.company_id)]
    assert actions == ["STRUCTURE_CREATED", "STRUCTURE_PUBLISHED"]


def test_ec_cmp_04_publish_reports_the_minimum_ctc_of_a_negative_balancing(acme):
    created = make_structure(acme, code="FLOORED", lines=FLOORED, publish=False,
                             test_range={"min": "300000", "max": "900000"})
    r = acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1})
    assert r.status_code == 400 and r.json()["code"] == "NEGATIVE_BALANCING"
    minimum = Decimal(r.json()["detail"]["minimum_pay_basis"]["amount"])
    assert Decimal("335999") <= minimum <= Decimal("336000")
    low = ok(preview(acme, created["id"], "300000", version=1))
    assert low["ok"] is False and any(i["component"] == "FIXED_ALLOWANCE" for i in low["issues"])
    version = created["versions"][0]
    ok(acme.put(f"{STRUCTURES}/{created['id']}/versions/1",
                json={"row_version": version["row_version"], "test_range": {"min": "336000", "max": "900000"}}))
    assert ok(acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}))["active_version"] == 1


def test_balancing_needs_a_declared_range_and_whole_range_negative_is_reported(acme):
    created = make_structure(acme, code="NORANGE", publish=False, test_range=None)
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={}))
    assert [e["code"] for e in report["errors"]] == ["TEST_RANGE_REQUIRED"]
    created = make_structure(acme, code="NEVER", lines=FLOORED, publish=False, test_range={"min": "1000", "max": "2000"})
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={}))
    assert report["errors"][0]["code"] == "NEGATIVE_BALANCING" and "whole range" in report["errors"][0]["message"]


def test_saved_test_vectors_must_pass_to_publish(acme):
    good = {"name": "Example A", "pay_basis_amount": "1200000", "expected": {"BASIC": "50000.00", "HRA": "20000.00"}}
    bad = {"name": "Wrong", "pay_basis_amount": "1200000", "expected": {"BASIC": "1.00"}}
    created = make_structure(acme, code="VECTORS", publish=False, test_vectors=[good, bad])
    r = acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1})
    assert r.status_code == 400 and r.json()["code"] == "TEST_VECTOR_FAILED"
    vectors = {v["name"]: v for v in r.json()["detail"]["vectors"]}
    assert vectors["Example A"]["passed"] is True
    assert vectors["Wrong"]["mismatches"] == [{"component": "BASIC", "expected": "1.00", "actual": "50000.00"}]
    version = created["versions"][0]
    ok(acme.put(f"{STRUCTURES}/{created['id']}/versions/1", json={"row_version": version["row_version"],
                                                                  "test_vectors": [good]}))
    published = ok(acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}))
    assert published["versions"][0]["validation"]["vectors"][0]["passed"] is True
    r = acme.post(f"{STRUCTURES}", json={"code": "BADVEC", "name": "Bad", "type": "CTC_BASED", "lines": EXAMPLE_A,
                                         "test_vectors": [{"pay_basis_amount": 1200000, "expected": {"BASIC": "1"}}]})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_DECIMAL"


def test_ec_cmp_01_structure_cycle_is_reported_with_its_path(acme):
    lines = [{"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 50%"},
             {"component": "HRA", "method": "FORMULA", "formula": "FIXED_ALLOWANCE * 10%"},
             {"component": "FIXED_ALLOWANCE", "method": "BALANCING"}]
    created = make_structure(acme, code="CYCLIC", lines=lines, publish=False)
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={}))
    cycle = next(e for e in report["errors"] if e["code"] == "CYCLE_DETECTED")
    assert cycle["path"][0] == cycle["path"][-1] and {"HRA", "FIXED_ALLOWANCE"} <= set(cycle["path"])
    r = acme.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1})
    assert r.status_code == 400 and r.json()["code"] == "CYCLE_DETECTED"


def test_structure_formula_errors_unknown_components_and_duplicates(acme):
    r = acme.post(STRUCTURES, json={"code": "X1", "name": "X", "type": "CTC_BASED",
                                    "lines": [{"component": "NOPE", "method": "FIXED", "value": "1"}]})
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_COMPONENT"
    r = acme.post(STRUCTURES, json={"code": "X2", "name": "X", "type": "CTC_BASED",
                                    "lines": [{"component": "BASIC", "value": "1"}, {"component": "BASIC", "value": "2"}]})
    assert r.status_code == 400 and r.json()["code"] == "DUPLICATE_COMPONENT"
    created = make_structure(acme, code="BADFORMULA", publish=False, lines=[
        {"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly + 5%"}], test_range=None)
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={}))
    assert report["ok"] is False and report["errors"][0]["code"] == "TYPE_MISMATCH"
    r = acme.post(STRUCTURES, json={"code": "lower case!", "name": "X", "type": "CTC_BASED", "lines": []})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_CODE"
    r = acme.post(STRUCTURES, json={"code": "X3", "name": "X", "type": "WEEKLY", "lines": []})
    assert r.status_code == 400
    r = acme.post(STRUCTURES, json={"code": "X4", "name": "X", "type": "NET_BASED", "lines": []})
    assert r.status_code == 400 and r.json()["code"] == "NET_BASED_DISABLED"


# --------------------------------------------------------------------------- versions
def test_published_versions_are_frozen_and_drafts_use_row_versions(acme):
    structure = make_structure(acme)
    r = acme.put(f"{STRUCTURES}/{structure['id']}/versions/1", json={"test_range": {"min": "1", "max": "2"}})
    assert r.status_code == 409 and r.json()["code"] == "VERSION_NOT_DRAFT"
    with pytest.raises(Exception, match="cannot change"):
        sql("UPDATE salary_template_lines SET value = 1 WHERE template_version_id = :v", v=active_version_id(structure))
    with pytest.raises(Exception, match="frozen"):
        sql("UPDATE salary_template_versions SET test_vectors = '[]'::jsonb, eligibility = '{\"x\": 1}'::jsonb "
            "WHERE id = :v", v=active_version_id(structure))
    draft = ok(acme.post(f"{STRUCTURES}/{structure['id']}/versions", json={}), 201)
    assert draft["draft_version"] == 2 and draft["active_version"] == 1
    r = acme.post(f"{STRUCTURES}/{structure['id']}/versions", json={})
    assert r.status_code == 409 and r.json()["code"] == "DRAFT_EXISTS"
    v2 = next(v for v in draft["versions"] if v["version"] == 2)
    ok(acme.put(f"{STRUCTURES}/{structure['id']}/versions/2", json={"row_version": v2["row_version"], "name": "Renamed"}))
    r = acme.put(f"{STRUCTURES}/{structure['id']}/versions/2", json={"row_version": v2["row_version"], "name": "Again"})
    assert r.status_code == 409 and r.json()["code"] == "ROW_VERSION_CONFLICT"


def test_ec_cmp_17_new_version_keeps_assignments_until_moved_with_a_preview(app, acme):
    team = people(app, acme)
    structure = make_structure(acme)
    v1 = active_version_id(structure)
    worker = team["worker"]["id"]
    assign_and_approve(acme, team["approver"], worker, v1, "1200000")
    draft = ok(acme.post(f"{STRUCTURES}/{structure['id']}/versions", json={}), 201)
    v2 = next(v for v in draft["versions"] if v["version"] == 2)
    lines = [{**line, "value": "50"} if line["component"] == "HRA" else line for line in EXAMPLE_A]
    ok(acme.put(f"{STRUCTURES}/{structure['id']}/versions/2", json={"row_version": v2["row_version"], "lines": lines}))
    published = ok(acme.post(f"{STRUCTURES}/{structure['id']}/publish", json={"version": 2}))
    statuses = {v["version"]: v["status"] for v in published["versions"]}
    assert statuses == {1: "ARCHIVED", 2: "ACTIVE"} and published["assignment_counts"] == {"1": 1, "2": 0}
    current = ok(acme.get(employee_url(worker)))["current"]
    assert current["structure"]["version"] == 1 and current["structure"]["version_status"] == "ARCHIVED"
    r = assign(acme, team["boss"]["id"], v1, "1000000")
    assert r.status_code == 409 and r.json()["code"] == "STRUCTURE_NOT_ACTIVE"  # an archived version is not assignable
    start = next_month(acme.company_id).isoformat()
    dry = ok(acme.post(f"{STRUCTURES}/{structure['id']}/move-assignments", json={"effective_from": start}))
    assert dry["dry_run"] is True and dry["count"] == 1 and dry["to_version"] == 2
    item = {line["component"]: line for line in dry["items"][0]["lines"]}
    assert item["HRA"]["before_monthly"]["amount"] == "20000.00" and item["HRA"]["after_monthly"]["amount"] == "25000.00"
    assert item["FIXED_ALLOWANCE"]["after_monthly"]["amount"] == "25000.00"
    assert ok(acme.get(employee_url(worker)))["current"]["structure"]["version"] == 1  # a dry run changes nothing
    moved = ok(acme.post(f"{STRUCTURES}/{structure['id']}/move-assignments",
                         json={"effective_from": start, "dry_run": False}))
    assert moved["count"] == 1
    approved = ok(approve(team["approver"], moved["created"][0]))
    assert approved["status"] == "SCHEDULED" and approved["structure"]["version"] == 2
    r = acme.post(f"{STRUCTURES}/{structure['id']}/move-assignments", json={"effective_from": start,
                                                                           "assignment_ids": [999999]})
    assert r.status_code == 404


# --------------------------------------------------------------------------- currency, eligibility
def test_ec_cmp_07_structure_currency_is_single_and_preview_has_no_implicit_fx(acme):
    local = entities(acme)["LE01"]
    gulf = gulf_entity(acme)
    created = make_structure(acme, code="MIXED", publish=False,
                             eligibility={"legal_entity_ids": [local["id"], gulf["id"]]})
    report = ok(acme.post(f"{STRUCTURES}/{created['id']}/validate", json={}))
    assert report["errors"][0]["code"] == "MIXED_CURRENCIES" and set(report["errors"][0]["currencies"]) == {"AED",
                                                                                                           "INR"}
    gulf_only = make_structure(acme, code="GULF", eligibility={"legal_entity_ids": [gulf["id"]]},
                               test_range={"min": "100000", "max": "900000"})
    assert ok(preview(acme, gulf_only["id"], "300000", currency="AED"))["currency"] == "AED"
    r = preview(acme, gulf_only["id"], "300000", currency="INR")
    assert r.status_code == 400 and r.json()["code"] == "CURRENCY_MISMATCH"
    r = acme.post(STRUCTURES, json={"code": "BADELIG", "name": "X", "type": "CTC_BASED", "lines": EXAMPLE_A,
                                    "eligibility": {"legal_entity_ids": [999999]}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_ELIGIBILITY"


def test_assignment_needs_an_eligible_structure(app, acme):
    team = people(app, acme)
    gulf = gulf_entity(acme)
    gulf_only = make_structure(acme, code="GULF", eligibility={"legal_entity_ids": [gulf["id"]]},
                               test_range={"min": "100000", "max": "900000"})
    r = assign(acme, team["worker"]["id"], active_version_id(gulf_only), "300000", currency="AED")
    assert r.status_code == 400 and r.json()["code"] == "NOT_ELIGIBLE"


def test_gross_structure_with_assignment_supplied_amounts(app, acme):
    team = people(app, acme)
    lines = [{"component": "BASIC", "method": "FIXED", "overridable": True},
             {"component": "HRA", "method": "FIXED", "overridable": True, "override_max": "100000"}]
    structure = make_structure(acme, code="GROSS", type_="GROSS_BASED", lines=lines, test_range=None)
    version_id, worker = active_version_id(structure), team["worker"]["id"]
    r = assign(acme, worker, version_id, "60000")
    assert r.status_code == 400 and r.json()["code"] == "MISSING_AMOUNT"
    created = ok(assign(acme, worker, version_id, "60000",
                        overrides={"BASIC": {"amount": "40000"}, "HRA": {"amount": "20000"}}), 201)
    ok(approve(team["approver"], created["id"]))
    rows = {row["component"]: row for row in ok(acme.get(employee_url(worker)))["breakdown"]["lines"]}
    assert rows["BASIC"]["monthly"]["amount"] == "40000.00" and rows["HRA"]["annual"]["amount"] == "240000.00"


# --------------------------------------------------------------------------- authorization and isolation
def test_authorization_of_structures(app, acme):
    from app.core.rate_limit import limiter

    limiter.reset()
    employee = invite_and_activate(app, acme, create_employee(acme, "Plain"), ["employee"])
    processor = invite_and_activate(app, acme, create_employee(acme, "Proc"), ["payroll_processor"])
    approver = invite_and_activate(app, acme, create_employee(acme, "Appr"), ["payroll_approver"])
    assert employee.get(STRUCTURES).status_code == 403
    assert employee.post(STRUCTURES, json={"code": "E", "name": "E", "type": "CTC_BASED", "lines": []}).status_code == 403
    created = make_structure(processor, code="BYPROC", publish=False)
    assert approver.get(STRUCTURES).status_code == 403  # approvers do not read structures by default
    assert approver.put(f"{STRUCTURES}/{created['id']}/versions/1", json={"name": "x"}).status_code == 403
    published = ok(approver.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}))
    assert published["active_version"] == 1  # the approver publishes a draft prepared by someone else
    assert employee.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}).status_code == 403
    archived = ok(processor.post(f"{STRUCTURES}/{created['id']}/archive"))
    assert archived["status"] == "ARCHIVED"
    assert created["id"] not in {s["id"] for s in ok(processor.get(STRUCTURES))}
    assert created["id"] in {s["id"] for s in ok(processor.get(STRUCTURES, params={"include_archived": True}))}


def test_cross_tenant_structures_are_not_found(app, acme):
    structure = make_structure(acme)
    other = signup_company(app, "Beta Industries")
    setup_catalogue(other)
    assert other.get(f"{STRUCTURES}/{structure['id']}").status_code == 404
    assert preview(other, structure["id"], "1200000").status_code == 404
    assert other.post(f"{STRUCTURES}/{structure['id']}/publish", json={"version": 1}).status_code == 404
    assert other.post(f"{STRUCTURES}/{structure['id']}/versions", json={}).status_code == 404
    assert other.post(f"{STRUCTURES}/{structure['id']}/move-assignments",
                      json={"effective_from": month_start(other.company_id).isoformat()}).status_code == 404
    assert structure["id"] not in {s["id"] for s in ok(other.get(STRUCTURES))}
