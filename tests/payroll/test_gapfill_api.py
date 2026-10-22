"""API gap-fill (F-43, F-44, F-25): additive endpoints, filters and fields for the payroll screens, each with
its positive case and the negative cases that keep it scope-safe (role, scope, other tenant).

* F-43: components `in_use`, formula catalogue, pack tag requirements, revision cycles created before the
  method is chosen, move-assignment previews with employee names, structure previews of unsaved lines.
* F-44: wage-input import list, legal-entity filter on wage inputs, period filter on tasks, claim types from the
  resolved reimbursement policy, employee name and code on wage inputs, loans, claims, overtime and tasks (never
  outside scope), per-module approval counts, upload limits.
* F-25: policy head counts (scoped), approval chains per module.
"""

from datetime import date, timedelta

import pytest

from app.payroll import formula as fm
from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_claims import POLICY as CLAIM_POLICY
from tests.payroll.test_compensation import approve as approve_compensation
from tests.payroll.test_compensation import assign_and_approve, next_month, people
from tests.payroll.test_components import (
    COMPONENTS,
    EXAMPLE_A,
    STRUCTURES,
    active_version_id,
    add_component,
    make_structure,
    ok,
    setup_catalogue,
)
from tests.payroll.test_loans import loan_body, loans_world  # noqa: F401  (loans_world is a fixture)
from tests.payroll.test_structures import acme  # noqa: F401  (acme is a fixture)
from tests.payroll.test_tasks import created_task, task_policy
from tests.payroll.test_wage_inputs import (  # noqa: F401  (world is a fixture)
    _csv,
    activate_policy,
    body,
    component,
    created,
    role,
    session,
    set_setting,
    sql,
    user_with_role,
    world,
)

PAY = "/api/v1/payroll"
INPUTS = f"{PAY}/wage-inputs"
IMPORTS = f"{PAY}/wage-input-imports"
CYCLES = f"{PAY}/revision-cycles"
CLAIM_TYPES = f"{PAY}/reimbursement-claim-types"
COUNTS = f"{PAY}/approvals/counts"
CHAINS = f"{PAY}/approval-chains"
POLICIES = f"{PAY}/policies"
TASKS = "/api/v1/tasks"
OVERTIME = "/api/v1/overtime/entries"


@pytest.fixture
def inputs_world(world):
    """The wage-input world with its input components. New tenants are provisioned with the country-neutral
    catalogue (BONUS, COMMISSION, BASIC, ... by component_seed), so the seeded components are reused and only
    the missing ones are added (never a second row with the same code). The seeded versions start on the signup
    day; like the components the wage-input suite creates, they are made effective from 2020-01-01 so inputs
    for the whole current month resolve them."""
    cid = world["cid"]
    wanted = {"BONUS": {}, "COMMISSION": {"category": "COMMISSION"},
              "CLAWBACK": {"params": {"allow_negative_input": True}},
              "OT_HOURS": {"method": "RATE_X_UNITS", "category": "OVERTIME"},
              "BASIC": {"method": "FIXED", "category": "BASE"}}
    seeded = dict(sql("SELECT code, id FROM pay_components WHERE company_id = :c", c=cid))
    reused = [seeded[code] for code in wanted if code in seeded]
    if reused:
        sql("UPDATE pay_component_versions SET effective_from = DATE '2020-01-01' WHERE company_id = :c "
            "AND component_id = ANY(:ids) AND version = 1", c=cid, ids=reused)
    world["components"] = {code: seeded.get(code) or component(cid, code, **extra) for code, extra in wanted.items()}
    return world


def entities(client):
    return {e["code"]: e for e in ok(client.get("/api/v1/legal-entities"))}


def gulf(client):
    return ok(client.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Gulf FZ-LLC",
                                                           "country_code": "AE", "base_currency": "AED",
                                                           "timezone": "Asia/Dubai"}))


def by(items, key, value):
    return next(item for item in items if item[key] == value)


# =========================================================================== F-43 components and formulas
def test_f43_component_list_reports_in_use_like_the_detail(app, acme):
    listing = {c["code"]: c for c in ok(acme.get(COMPONENTS))}
    assert listing and all(c["in_use"] is False for c in listing.values())
    make_structure(acme)
    listing = {c["code"]: c for c in ok(acme.get(COMPONENTS))}
    used = {line["component"] for line in EXAMPLE_A}
    assert {code for code, c in listing.items() if c["in_use"]} == used
    for code in (*used, next(c for c in listing if c not in used)):
        assert ok(acme.get(f"{COMPONENTS}/{code}"))["in_use"] is listing[code]["in_use"]
    assert ok(acme.get(COMPONENTS, params={"kind": "EARNING"}))[0]["in_use"] in (True, False)
    # Other tenants see their own catalogue only; people without structure access get 403.
    beta = signup_company(app, "Beta Industries")
    setup_catalogue(beta)
    assert all(c["in_use"] is False for c in ok(beta.get(COMPONENTS)))
    worker = invite_and_activate(app, acme, create_employee(acme, "Wes"), ["employee"])
    assert worker.get(COMPONENTS).status_code == 403


def _table(company_id, code, owner="COMPANY", status="ACTIVE", kind="SLAB", currency="INR"):
    from app.models.payroll_config import PayrollTable

    with session(company_id) as (db, _):
        db.add(PayrollTable(company_id=company_id, code=code, name=f"Table {code}", kind=kind, currency=currency,
                            owner=owner, effective_from=date(2020, 1, 1), status=status))
        db.commit()


def test_f43_formula_catalogue_comes_from_the_language_and_lists_tables(app, acme):
    beta = signup_company(app, "Beta Industries")
    _table(acme.company_id, "PT_SLABS")
    _table(acme.company_id, "PACK_BANDS", owner="PACK", kind="BAND")
    _table(acme.company_id, "DRAFT_TABLE", status="DRAFT")
    _table(beta.company_id, "BETA_ONLY")
    catalogue = ok(acme.get(f"{PAY}/formula/catalogue"))
    assert [f["name"] for f in catalogue["functions"]] == list(fm.FUNCTIONS)
    rounding = by(catalogue["functions"], "name", "ROUND")
    assert rounding["arity"] == {"min": 2, "max": 3} and rounding["quoted_arguments"] == {"2": "MODE"}
    assert by(catalogue["functions"], "name", "MAX")["arity"] == {"min": 2, "max": None}
    variables = {v["name"]: v for v in catalogue["variables"]}
    assert variables["att.paid_days"]["namespace"] == "att" and variables["att.paid_days"]["type"] == "DECIMAL"
    assert variables["ot.hourly_rate"]["type"] == "MONEY" and variables["ot.hourly_rate"]["pay_currency"] is True
    assert variables["ot.hourly_rate"]["source_setting"] == "payroll.overtime.enabled"
    expected = {name for name, v in fm.VARIABLES.items() if not v.protected}
    assert set(variables) == expected
    assert "emp.gender" not in variables and "emp.age" not in variables  # EC-CMP-19: never offered
    assert catalogue["namespaces"] == list(fm.NAMESPACES)
    tables = {t["code"]: t for t in catalogue["tables"]}
    assert set(tables) == {"PT_SLABS", "PACK_BANDS"}  # active only, own tenant only
    assert tables["PACK_BANDS"]["owner"] == "PACK" and tables["PT_SLABS"]["kind"] == "SLAB"
    assert "BASIC" in {c["code"] for c in catalogue["components"]} and "PART_OF_CTC" in catalogue["tags"]
    assert {t["code"] for t in ok(beta.get(f"{PAY}/formula/catalogue"))["tables"]} == {"BETA_ONLY"}
    worker = invite_and_activate(app, acme, create_employee(acme, "Wes"), ["employee"])
    assert worker.get(f"{PAY}/formula/catalogue").status_code == 403


def test_f43_pack_tag_requirements_are_labelled_and_scoped(app, acme):
    local = entities(acme)["LE01"]
    assert local["country_code"] == "IN"
    middle_east = gulf(acme)
    data = ok(acme.get(f"{PAY}/packs/tag-requirements"))
    items = {i["legal_entity"]["id"]: i for i in data["items"]}
    assert set(items) == {local["id"], middle_east["id"]}
    india = items[local["id"]]["packs"][0]
    assert india["is_fixture"] is True and india["label"] == "FIXTURE — not law" and india["verified"] is False
    tags = {r["tag"]: r for r in india["requirements"]}
    assert {"IN_PF_WAGE", "IN_ESI_WAGE", "TAXABLE_INCOME"} <= set(tags)
    assert tags["IN_PF_WAGE"]["used_for"] == "STATUTORY_BASE" and tags["IN_PF_WAGE"]["satisfied"] is False
    assert "IN_PF_WAGE" in india["missing"]
    uae = items[middle_east["id"]]["packs"][0]
    assert uae["status"] == "DRAFT" and uae["label"] == "Compliance not verified" and uae["is_fixture"] is False
    assert [r["tag"] for r in uae["requirements"]] == ["AE_GPSSA_WAGE"]  # no income tax: no TAXABLE_INCOME

    ok(add_component(acme, "PF_BASIC", tags=["IN_PF_WAGE"]), 201)
    single = ok(acme.get(f"{PAY}/packs/tag-requirements", params={"legal_entity_id": local["id"]}))
    assert [i["legal_entity"]["id"] for i in single["items"]] == [local["id"]]
    pf = by(single["items"][0]["packs"][0]["requirements"], "tag", "IN_PF_WAGE")
    assert pf["satisfied"] is True and pf["components"] == ["PF_BASIC"]

    assert acme.get(f"{PAY}/packs/tag-requirements", params={"legal_entity_id": 999999}).status_code == 404
    beta = signup_company(app, "Beta Industries")
    assert beta.get(f"{PAY}/packs/tag-requirements",
                    params={"legal_entity_id": local["id"]}).status_code == 404  # another tenant's entity
    reader = role(acme, "Entity structure reader", "LEGAL_ENTITY", ("payroll:salary_structure:read",))
    scoped = user_with_role(app, acme, create_employee(acme, "Sana"), reader, [local["id"]])
    assert [i["legal_entity"]["id"] for i in ok(scoped.get(f"{PAY}/packs/tag-requirements"))["items"]] == \
        [local["id"]]
    assert scoped.get(f"{PAY}/packs/tag-requirements",
                      params={"legal_entity_id": middle_east["id"]}).status_code == 404
    worker = invite_and_activate(app, acme, create_employee(acme, "Wes"), ["employee"])
    assert worker.get(f"{PAY}/packs/tag-requirements").status_code == 403


# =========================================================================== F-43 compensation
def test_f43_revision_cycle_is_created_before_the_method_and_previews_only_with_one(app, acme):
    team = people(app, acme)
    start = next_month(acme.company_id).isoformat()
    cycle = ok(acme.post(CYCLES, json={"name": "FY27 merit", "effective_from": start}), 201)
    assert cycle["status"] == "DRAFT" and cycle["method"] == {}
    r = acme.post(f"{CYCLES}/{cycle['id']}/preview")
    assert r.status_code == 400 and r.json()["code"] == "METHOD_REQUIRED"
    updated = ok(acme.put(f"{CYCLES}/{cycle['id']}", json={"row_version": cycle["row_version"],
                                                           "method": {"type": "PERCENT", "percent": "8"}}))
    assert updated["method"] == {"type": "PERCENT", "percent": "8"}
    assert ok(acme.post(f"{CYCLES}/{cycle['id']}/preview"))["status"] == "PREVIEWED"
    # The full method is still accepted (and validated) on create.
    full = ok(acme.post(CYCLES, json={"name": "Fixed", "effective_from": start,
                                      "method": {"type": "FIXED_AMOUNT", "amounts": {"INR": "5000"}}}), 201)
    assert full["method"] == {"type": "FIXED_AMOUNT", "amounts": {"INR": "5000"}}
    r = acme.post(CYCLES, json={"name": "Bad", "effective_from": start, "method": {"type": "GUESS"}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_METHOD"
    r = acme.post(CYCLES, json={"name": "Bad", "effective_from": start, "method": {"type": "PERCENT", "x": 1}})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_METHOD"
    assert team["approver"].post(CYCLES, json={"name": "No", "effective_from": start}).status_code == 403
    assert team["employee_client"].post(CYCLES, json={"name": "No", "effective_from": start}).status_code == 403


def test_f43_move_assignment_preview_names_only_employees_in_scope(app, acme):
    team = people(app, acme)
    structure = make_structure(acme)
    worker = team["worker"]
    assign_and_approve(acme, team["approver"], worker["id"], active_version_id(structure), "1200000")
    draft = ok(acme.post(f"{STRUCTURES}/{structure['id']}/versions", json={}), 201)
    v2 = next(v for v in draft["versions"] if v["version"] == 2)
    lines = [{**line, "value": "50"} if line["component"] == "HRA" else line for line in EXAMPLE_A]
    ok(acme.put(f"{STRUCTURES}/{structure['id']}/versions/2", json={"row_version": v2["row_version"], "lines": lines}))
    ok(acme.post(f"{STRUCTURES}/{structure['id']}/publish", json={"version": 2}))
    start = next_month(acme.company_id).isoformat()
    dry = ok(acme.post(f"{STRUCTURES}/{structure['id']}/move-assignments", json={"effective_from": start}))
    [item] = dry["items"]
    assert item["employee_id"] == worker["id"] and item["employee_name"] == "Wendy Worker"
    assert item["employee_code"] == worker["employee_code"]
    assert item["employee"] == {"id": worker["id"], "employee_code": worker["employee_code"], "name": "Wendy Worker"}
    # A compensation editor scoped to another legal entity sees no item, so no name.
    middle_east = gulf(acme)
    editor_role = role(acme, "Gulf compensation", "LEGAL_ENTITY",
                       ("payroll:compensation:edit", "payroll:salary_structure:read"))
    editor = user_with_role(app, acme, create_employee(acme, "Gil"), editor_role, [middle_east["id"]])
    r = editor.post(f"{STRUCTURES}/{structure['id']}/move-assignments", json={"effective_from": start})
    assert r.status_code == 200 and r.json()["count"] == 0 and "Wendy" not in r.text
    moved = ok(acme.post(f"{STRUCTURES}/{structure['id']}/move-assignments",
                         json={"effective_from": start, "dry_run": False}))
    assert moved["items"][0]["employee_code"] == worker["employee_code"]
    assert ok(approve_compensation(team["approver"], moved["created"][0]))["status"] == "SCHEDULED"


def _preview(client, structure_id, lines, amount="1200000", **extra):
    return client.post(f"{STRUCTURES}/{structure_id}/preview",
                       json={"pay_basis_amount": {"amount": amount, "currency": "INR"}, "lines": lines, **extra})


def test_f43_structure_preview_of_unsaved_lines_is_validated_and_never_saved(app, acme):
    structure = make_structure(acme)
    unsaved = [{"component": "BASIC", "method": "FIXED", "value": "30000"},
               {"component": "HRA", "method": "PERCENT_OF", "value": "50", "params": {"bases": ["BASIC"]}}]
    result = ok(_preview(acme, structure["id"], unsaved))
    rows = {row["component"]: row for row in result["lines"]}
    assert set(rows) == {"BASIC", "HRA"} and result["unsaved"] is True
    assert rows["BASIC"]["monthly"]["amount"] == "30000.00" and rows["HRA"]["monthly"]["amount"] == "15000.00"
    saved = ok(acme.post(f"{STRUCTURES}/{structure['id']}/preview",
                         json={"pay_basis_amount": {"amount": "1200000", "currency": "INR"}}))
    assert saved["unsaved"] is False and {r["component"] for r in saved["lines"]} == {l["component"] for l in EXAMPLE_A}
    stored = ok(acme.get(f"{STRUCTURES}/{structure['id']}"))["versions"][0]["lines"]
    assert [line["component"] for line in stored] == [line["component"] for line in EXAMPLE_A]
    assert sql("SELECT count(*) FROM audit_logs WHERE company_id = :c AND action = 'STRUCTURE_VERSION_UPDATED'",
               c=acme.company_id) == [(0,)]

    r = _preview(acme, structure["id"], [{"component": "NOPE", "method": "FIXED", "value": "1"}])
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_COMPONENT"
    r = _preview(acme, structure["id"], [{"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly *"}])
    assert r.status_code == 400 and r.json()["detail"]["errors"]  # a parse error is a 400, never a 500
    r = _preview(acme, structure["id"], [{"component": "BASIC", "method": "FORMULA", "formula": "emp.grade + 1"}])
    assert r.status_code == 400 and r.json()["detail"]["errors"]  # type errors block the preview
    r = _preview(acme, structure["id"], [{"component": "BASIC", "method": "FIXED", "value": 30000}])
    assert r.status_code == 422  # never a JSON number
    beta = signup_company(app, "Beta Industries")
    setup_catalogue(beta)
    assert _preview(beta, structure["id"], unsaved).status_code == 404
    worker = invite_and_activate(app, acme, create_employee(acme, "Wes"), ["employee"])
    assert _preview(worker, structure["id"], unsaved).status_code == 403


# =========================================================================== F-44 wage inputs
def test_f44_wage_inputs_carry_names_in_scope_and_filter_by_legal_entity(inputs_world):
    w = inputs_world
    pia, cleo, founder = w["u"]["pia"], w["u"]["cleo"], w["u"]["founder"]
    [emma_input] = created(pia, body(w))
    [gus_input] = created(pia, body(w, employee="Gus", amount="700.00", currency="AED"))
    listing = ok(pia.get(INPUTS))
    rows = {row["id"]: row for row in listing["items"]}
    assert rows[emma_input["id"]]["employee_name"] == "Emma Test"
    assert rows[emma_input["id"]]["employee_code"] == w["codes"]["Emma"]
    assert rows[gus_input["id"]]["employee_code"] == w["codes"]["Gus"]
    detail = ok(pia.get(f"{INPUTS}/{emma_input['id']}"))
    assert (detail["employee_name"], detail["employee_code"]) == ("Emma Test", w["codes"]["Emma"])

    by_entity = ok(founder.get(INPUTS, params={"legal_entity_id": w["ae"]["id"]}))
    assert [row["id"] for row in by_entity["items"]] == [gus_input["id"]] and by_entity["total"] == 1
    assert [row["id"] for row in ok(founder.get(INPUTS, params={"legal_entity_id": w["le01"]["id"]}))["items"]] \
        == [emma_input["id"]]
    # Cleo's scope is LE01: Gus's input, and his name, never reach her.
    r = cleo.get(INPUTS)
    assert r.status_code == 200 and {row["id"] for row in r.json()["items"]} == {emma_input["id"]}
    assert "Gus" not in r.text and w["codes"]["Gus"] not in r.text
    assert ok(cleo.get(INPUTS, params={"legal_entity_id": w["ae"]["id"]}))["total"] == 0
    assert cleo.get(f"{INPUTS}/{gus_input['id']}").status_code == 404
    assert w["u"]["beta"].get(INPUTS, params={"legal_entity_id": w["ae"]["id"]}).json()["total"] == 0
    assert w["u"]["emma"].get(INPUTS).status_code == 403
    assert founder.get(INPUTS, params={"legal_entity_id": 0}).status_code == 422


def test_f44_wage_input_imports_are_listed_within_the_import_scope(inputs_world):
    w = inputs_world
    pia, cleo = w["u"]["pia"], w["u"]["cleo"]
    s, e = (d.isoformat() for d in w["period"])
    good = _csv(f"{w['codes']['Emma']},BONUS,1000.00,,,INR,{s},{e},IMP-1,Festival")
    bad = _csv(f"NOBODY,BONUS,1000.00,,,INR,{s},{e},IMP-2,")
    first = pia.post(f"{INPUTS}/import", files={"file": ("pia.csv", good, "text/csv")}).json()
    second = cleo.post(f"{INPUTS}/import", files={"file": ("cleo.csv", bad, "text/csv")}).json()
    assert (first["status"], second["status"]) == ("VALIDATED", "INVALID")

    listing = ok(pia.get(IMPORTS))
    assert listing["total"] == 2 and [i["id"] for i in listing["items"]] == [second["id"], first["id"]]
    item = by(listing["items"], "id", first["id"])
    assert item["file_name"] == "pia.csv" and item["row_count"] == 1 and "rows" not in item
    assert ok(pia.get(IMPORTS, params={"status": "INVALID"}))["items"][0]["id"] == second["id"]
    assert ok(pia.get(IMPORTS, params={"limit": 1, "skip": 1}))["items"][0]["id"] == first["id"]
    # Entity-scoped import access lists only the caller's own imports (as GET /{id} does).
    assert [i["id"] for i in ok(cleo.get(IMPORTS))["items"]] == [second["id"]]
    assert cleo.get(f"{IMPORTS}/{first['id']}").status_code == 404
    assert pia.get(IMPORTS, params={"status": "NOPE"}).status_code == 422
    assert w["u"]["emma"].get(IMPORTS).status_code == 403
    assert ok(w["u"]["beta"].get(IMPORTS))["total"] == 0  # Beta's payroll manager sees only Beta's imports
    assert pia.get(f"{IMPORTS}/template").status_code == 200  # the template route still matches


# =========================================================================== F-44 claims and loans
@pytest.fixture
def claims_ready(world):
    set_setting(world["cid"], "payroll.reimbursements.enabled", True)
    activate_policy(world["u"]["founder"], "REIMBURSEMENT_POLICY", CLAIM_POLICY)
    return world


def test_f44_claim_types_come_from_the_resolved_policy_for_the_caller_or_an_employee(claims_ready):
    w = claims_ready
    emma, pia, cleo, gus = w["u"]["emma"], w["u"]["pia"], w["u"]["cleo"], w["u"]["gus"]
    own = ok(emma.get(CLAIM_TYPES))
    assert own["status"] == "RESOLVED" and own["employee_id"] == w["emp"]["Emma"] and own["enabled"] is True
    assert [t["code"] for t in own["claim_types"]] == ["MEALS", "TRAVEL"]
    meals = own["claim_types"][0]
    assert meals["limit_per_claim"] == {"amount": "1000.00", "currency": "INR"} and meals["taxability"] == \
        "UNCLASSIFIED"
    assert own["claim_types"][1]["taxability"] == "EXEMPT" and own["submission_window_days"] == 60
    assert ok(emma.get(CLAIM_TYPES, params={"employee_id": w["emp"]["Emma"]}))["claim_types"] == own["claim_types"]
    staff = ok(pia.get(CLAIM_TYPES, params={"employee_id": w["emp"]["Gus"]}))
    assert staff["employee_id"] == w["emp"]["Gus"] and staff["currency"] == "AED"
    assert ok(cleo.get(CLAIM_TYPES, params={"employee_id": w["emp"]["Emma"]}))["status"] == "RESOLVED"
    # Outside the caller's scope, another tenant or an unknown employee: 404; no SELF grant at all: 403.
    assert cleo.get(CLAIM_TYPES, params={"employee_id": w["emp"]["Gus"]}).status_code == 404
    assert gus.get(CLAIM_TYPES, params={"employee_id": w["emp"]["Emma"]}).status_code == 404
    assert w["u"]["beta"].get(CLAIM_TYPES, params={"employee_id": w["emp"]["Emma"]}).status_code == 404
    assert pia.get(CLAIM_TYPES, params={"employee_id": 999999}).status_code == 404
    # Provisioning places the founder in the default pay group: their own types resolve like anyone's ...
    assert ok(w["u"]["founder"].get(CLAIM_TYPES))["status"] == "RESOLVED"
    # ... and a profile that is in no pay group gets 409 NO_PAY_GROUP, not an empty list.
    sql("UPDATE employee_assignments SET pay_group_id = NULL WHERE company_id = :c AND employee_id = "
        "(SELECT id FROM employees WHERE company_id = :c AND user_id = :u)", c=w["cid"], u=w["u"]["founder"].user_id)
    r = w["u"]["founder"].get(CLAIM_TYPES)
    assert r.status_code == 409 and r.json()["code"] == "NO_PAY_GROUP"
    set_setting(w["cid"], "payroll.reimbursements.enabled", False)
    assert ok(emma.get(CLAIM_TYPES))["enabled"] is False


def test_f44_claims_and_loans_carry_names_only_in_scope(claims_ready, loans_world):  # noqa: F811
    w = loans_world
    emma, pia, cleo, gus = w["u"]["emma"], w["u"]["pia"], w["u"]["cleo"], w["u"]["gus"]
    claim = emma.post("/api/v1/me/payroll/reimbursement-claims", json={
        "claim_type": "TRAVEL", "expense_date": (w["today"] - timedelta(days=1)).isoformat(),
        "amount": {"amount": "300.00", "currency": "INR"}})
    assert claim.status_code == 201, claim.text
    claim = claim.json()
    rows = ok(pia.get(f"{PAY}/reimbursement-claims"))["items"]
    assert (rows[0]["employee_name"], rows[0]["employee_code"]) == ("Emma Test", w["codes"]["Emma"])
    detail = ok(cleo.get(f"{PAY}/reimbursement-claims/{claim['id']}"))
    assert detail["employee_code"] == w["codes"]["Emma"]
    mine = ok(emma.get(f"/api/v1/me/payroll/reimbursement-claims"))["items"]
    assert mine[0]["employee_name"] == "Emma Test"
    assert ok(gus.get("/api/v1/me/payroll/reimbursement-claims"))["total"] == 0
    assert gus.get(f"{PAY}/reimbursement-claims/{claim['id']}").status_code == 404
    assert "Emma" not in w["u"]["beta"].get(f"{PAY}/reimbursement-claims").text

    loan = pia.post(f"{PAY}/loans", json=loan_body(w))
    assert loan.status_code == 201, loan.text
    loan = loan.json()
    rows = ok(pia.get(f"{PAY}/loans"))["items"]
    assert (rows[0]["employee_name"], rows[0]["employee_code"]) == ("Emma Test", w["codes"]["Emma"])
    assert ok(pia.get(f"{PAY}/loans/{loan['id']}"))["employee_code"] == w["codes"]["Emma"]
    assert ok(cleo.get(f"{PAY}/loans"))["items"][0]["employee_name"] == "Emma Test"
    assert ok(emma.get("/api/v1/me/payroll/loans"))["items"][0]["employee_code"] == w["codes"]["Emma"]
    assert ok(gus.get("/api/v1/me/payroll/loans"))["total"] == 0
    assert gus.get(f"{PAY}/loans/{loan['id']}").status_code == 404
    beta = w["u"]["beta"].get(f"{PAY}/loans")
    assert beta.status_code == 200 and beta.json()["total"] == 0 and "Emma" not in beta.text


# =========================================================================== F-44 overtime and tasks
@pytest.fixture
def team(app):
    """Acme: founder admin; manager Mona -> Walt; Lou outside Mona's team. Beta: its admin."""
    admin = signup_company(app, "Acme Corp")
    beta = signup_company(app, "Beta Industries")
    mona = create_employee(admin, "Mona")
    walt = create_employee(admin, "Walt", manager_id=mona["id"])
    lou = create_employee(admin, "Lou")
    users = {"admin": admin, "beta": beta, "mona": invite_and_activate(app, admin, mona, ["manager"]),
             "walt": invite_and_activate(app, admin, walt, ["employee"]),
             "lou": invite_and_activate(app, admin, lou, ["employee"])}
    from tests.payroll.test_wage_inputs import today_of

    return {"cid": admin.company_id, "today": today_of(admin.company_id), "u": users,
            "emp": {"mona": mona, "walt": walt, "lou": lou}}


def test_f44_overtime_entries_carry_the_employee_code(team):
    u, emp = team["u"], team["emp"]
    day = (team["today"] - timedelta(days=1)).isoformat()
    walt_entry = u["walt"].post(OVERTIME, json={"date": day, "minutes": 60, "reason": "Release"})
    lou_entry = u["lou"].post(OVERTIME, json={"date": day, "minutes": 60, "reason": "Release"})
    assert walt_entry.status_code == lou_entry.status_code == 201
    listing = u["mona"].get(OVERTIME)
    [row] = listing.json()["items"]
    assert (row["employee_name"], row["employee_code"]) == ("Walt Test", emp["walt"]["employee_code"])
    assert emp["lou"]["employee_code"] not in listing.text  # outside Mona's team
    detail = ok(u["walt"].get(f"{OVERTIME}/{walt_entry.json()['id']}"))
    assert detail["employee_code"] == emp["walt"]["employee_code"]
    assert u["mona"].get(f"{OVERTIME}/{lou_entry.json()['id']}").status_code == 404
    assert u["beta"].get(OVERTIME).json()["total"] == 0


def test_f44_task_assignees_are_named_only_inside_the_callers_scope(team):
    u, emp = team["u"], team["emp"]
    task_policy(u["admin"], allow_split=True, requires_evidence=False)
    shared = created_task(u["admin"], [{"employee_id": emp["walt"]["id"], "split_percent": "50"},
                                       {"employee_id": emp["lou"]["id"], "split_percent": "50"}])
    full = {a["employee_id"]: a for a in ok(u["admin"].get(f"{TASKS}/{shared['id']}"))["assignees"]}
    assert full[emp["lou"]["id"]]["employee_name"] == "Lou Test"
    assert full[emp["walt"]["id"]]["employee_code"] == emp["walt"]["employee_code"]
    # Mona sees the task through Walt; Lou (outside her team) stays an id only, in the list and the detail.
    seen = ok(u["mona"].get(f"{TASKS}/{shared['id']}"))
    assignees = {a["employee_id"]: a for a in seen["assignees"]}
    assert assignees[emp["walt"]["id"]]["employee_name"] == "Walt Test"
    lou = assignees[emp["lou"]["id"]]
    assert lou["employee_name"] is None and lou["employee_code"] is None and lou["split_percent"] == "50.00"
    listing = u["mona"].get(TASKS)
    assert listing.status_code == 200 and "Lou Test" not in listing.text
    assert emp["lou"]["employee_code"] not in listing.text
    own = {a["employee_id"]: a for a in ok(u["lou"].get(f"{TASKS}/{shared['id']}"))["assignees"]}
    assert own[emp["lou"]["id"]]["employee_name"] == "Lou Test" and own[emp["walt"]["id"]]["employee_name"] is None


def test_f44_tasks_filter_by_period_on_the_chosen_date(team):
    u, emp, today = team["u"], team["emp"], team["today"]
    walt = emp["walt"]["id"]
    recent = created_task(u["admin"], [walt], completed_on=today.isoformat(), due_date=today.isoformat())
    older = created_task(u["admin"], [walt], completed_on=(today - timedelta(days=40)).isoformat())
    undated = created_task(u["admin"], [walt])

    def ids(**params):
        r = u["admin"].get(TASKS, params=params)
        assert r.status_code == 200, r.text
        return {t["id"] for t in r.json()["items"]}

    assert ids() >= {recent["id"], older["id"], undated["id"]}
    window = {"from": (today - timedelta(days=7)).isoformat(), "to": today.isoformat()}
    assert ids(**window) == {recent["id"]}
    assert ids(**{"from": (today - timedelta(days=60)).isoformat()}) == {recent["id"], older["id"]}
    assert ids(to=(today - timedelta(days=30)).isoformat()) == {older["id"]}
    assert ids(date_field="due_date", **window) == {recent["id"]}
    assert ids(date_field="created_at", **window) >= {recent["id"], older["id"], undated["id"]}
    assert ids(date_field="approved_on", **window) == set()
    r = u["admin"].get(TASKS, params={"from": today.isoformat(), "to": (today - timedelta(days=1)).isoformat()})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_PERIOD"
    assert u["admin"].get(TASKS, params={"date_field": "title", **window}).status_code == 422
    assert u["admin"].get(TASKS, params={"from": "not-a-date"}).status_code == 422
    # The filter never widens visibility.
    r = u["lou"].get(TASKS, params=window)
    assert r.status_code == 200 and r.json()["total"] == 0
    assert u["beta"].get(TASKS, params=window).json()["total"] == 0


# =========================================================================== F-44 approvals and uploads
def test_f44_approval_counts_per_module_follow_the_inbox_rules(inputs_world):
    w = inputs_world
    pia, cleo, founder = w["u"]["pia"], w["u"]["cleo"], w["u"]["founder"]
    created(pia, body(w))  # Emma (LE01), made by Pia
    created(founder, body(w, employee="Gus", amount="700.00", currency="AED"))  # Gus (AE01), made by the founder
    counts = ok(founder.get(COUNTS))
    assert counts["mine"] is True and set(counts["counts"]) >= {"wage_input", "loan", "overtime", "task"}
    assert counts["counts"]["wage_input"] == 1 and counts["total"] == 1  # the founder made Gus's input
    assert ok(pia.get(COUNTS))["counts"]["wage_input"] == 1  # Gus's only: the maker never approves her own
    assert ok(cleo.get(COUNTS))["counts"]["wage_input"] == 1  # Emma's only: Gus is outside LE01
    assert ok(pia.get(COUNTS, params={"mine": False}))["counts"]["wage_input"] == 2
    assert ok(cleo.get(COUNTS, params={"mine": False}))["counts"]["wage_input"] == 1
    assert ok(founder.get(COUNTS, params={"status": "APPROVED"}))["total"] == 0
    assert ok(founder.get(COUNTS, params={"mine": False, "status": "APPROVED"}))["total"] == 0
    listing = ok(cleo.get(f"{PAY}/approvals", params={"module": "wage_input"}))
    assert listing["total"] == ok(cleo.get(COUNTS))["counts"]["wage_input"]
    assert ok(w["u"]["emma"].get(COUNTS))["total"] == 0
    assert ok(w["u"]["beta"].get(COUNTS, params={"mine": False}))["total"] == 0
    assert founder.get(COUNTS, params={"status": "NOPE"}).status_code == 422


def test_f44_upload_limits_are_exposed_to_signed_in_users(app, inputs_world):
    from app.config import settings
    from app.payroll.services import wage_inputs

    limits = ok(inputs_world["u"]["emma"].get(f"{PAY}/uploads/limits"))
    assert limits["max_upload_bytes"] == settings.max_upload_size
    assert limits["kinds"]["wage_input_import"]["max_bytes"] == wage_inputs.MAX_IMPORT_BYTES
    assert limits["kinds"]["claim_receipt"]["mime_types"] == ["application/pdf", "image/jpeg", "image/png"]
    assert limits["kinds"]["task_evidence"]["max_files"] == 20
    from tests.helpers import ApiClient

    assert ApiClient(app).get(f"{PAY}/uploads/limits").status_code == 401


# =========================================================================== F-25 policies and chains
def test_f25_policy_head_counts_are_scoped(app, inputs_world):
    w = inputs_world
    founder = w["u"]["founder"]
    r = founder.post(POLICIES, json={"type": "REIMBURSEMENT_POLICY", "name": "Gulf claims",
                                     "fields": CLAIM_POLICY, "effective_from": "2020-01-01"})
    assert r.status_code == 201, r.text
    gulf_policy = r.json()["id"]
    r = founder.put(f"{POLICIES}/{gulf_policy}/assignments", json={"rules": [
        {"criteria": {"legal_entity_ids": [w["ae"]["id"]]}, "priority": 10, "effective_from": "2020-01-01"}]})
    assert r.status_code == 200, r.text
    listing = {p["id"]: p for p in ok(founder.get(POLICIES, params={"type": "REIMBURSEMENT_POLICY"}))}
    assert listing[gulf_policy]["head_count"] == 1 and listing[gulf_policy]["head_count_scope"] == "COMPANY"
    starter = next(p for pid, p in listing.items() if pid != gulf_policy)
    placed = sql("SELECT count(DISTINCT a.employee_id) FROM employee_assignments a JOIN employees e ON e.id = "
                 "a.employee_id WHERE a.company_id = :c AND a.is_primary AND e.is_active AND a.effective_from <= "
                 ":d AND (a.effective_to IS NULL OR a.effective_to >= :d)", c=w["cid"], d=w["today"])[0][0]
    assert starter["head_count"] == placed - 1 and starter["head_count"] >= 4
    everything = ok(founder.get(POLICIES))
    assert all(isinstance(p["head_count"], int) for p in everything)

    reader = role(founder, "Entity config reader", "LEGAL_ENTITY", ("payroll:configuration:read",))
    scoped = user_with_role(app, founder, create_employee(founder, "Rhea"), reader, [w["le01"]["id"]])
    seen = {p["id"]: p for p in ok(scoped.get(POLICIES, params={"type": "REIMBURSEMENT_POLICY"}))}
    assert seen[gulf_policy]["head_count"] == 0 and seen[gulf_policy]["head_count_scope"] == "SCOPED"
    assert seen[starter["id"]]["head_count"] == starter["head_count"] + 1  # Rhea joined LE01 meanwhile
    assert w["u"]["emma"].get(POLICIES).status_code == 403
    beta = ok(w["u"]["beta"].get(POLICIES))
    assert gulf_policy not in {p["id"] for p in beta}
    assert sum(p["head_count"] for p in beta if p["type"] == "REIMBURSEMENT_POLICY") == 1  # Beta's founder only


def test_f25_approval_chains_are_listed_per_module(app, inputs_world):
    w = inputs_world
    founder, pia = w["u"]["founder"], w["u"]["pia"]
    hr_role = sql("SELECT id FROM roles WHERE company_id = :c AND system_key = 'hr_admin'", c=w["cid"])[0][0]
    sql("INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, role_id, created_at, "
        "updated_at) VALUES (:c, 'overtime', 1, 'role', :r, now(), now())", c=w["cid"], r=hr_role)
    sql("INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, specific_user_id, "
        "min_amount, min_amount_currency, created_at, updated_at) VALUES (:c, 'overtime', 2, 'specific_user', :u, "
        "50000, 'INR', now(), now())", c=w["cid"], u=pia.user_id)
    chains = ok(founder.get(CHAINS))
    modules = [c["module"] for c in chains]
    assert "leave" not in modules and {"payroll_run", "wage_input", "overtime", "loan"} <= set(modules)
    overtime = by(chains, "module", "overtime")
    first = sql("SELECT id FROM approval_chain_steps WHERE company_id = :c AND module = 'overtime' AND "
                "step_order = 1", c=w["cid"])[0][0]
    assert overtime["configured"] is True and overtime["id"] == first and overtime["default_step"] is None
    assert [s["approver_type"] for s in overtime["steps"]] == ["role", "specific_user"]
    assert overtime["steps"][0]["role_name"] == "HR Administrator"
    assert overtime["steps"][1]["specific_user_name"] and overtime["steps"][1]["min_amount"] == {
        "amount": "50000.00", "currency": "INR"}
    wage = by(chains, "module", "wage_input")
    assert wage["configured"] is False and wage["id"] is None and wage["steps"] == []
    assert wage["default_step"]["approver_type"] == "permission_holders"
    assert wage["permission"] == "payroll:inputs:approve"
    assert [c["module"] for c in ok(pia.get(CHAINS, params={"module": "overtime"}))] == ["overtime"]
    assert pia.get(CHAINS, params={"module": "leave"}).status_code == 422
    assert w["u"]["emma"].get(CHAINS).status_code == 403
    assert w["u"]["cleo"].get(CHAINS).status_code == 403  # no payroll configuration access
    assert by(ok(w["u"]["beta"].get(CHAINS)), "module", "overtime")["configured"] is False  # tenant isolation
