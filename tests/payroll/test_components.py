"""Pay components: catalogue, versions, formula validation, tester, seeding, isolation (3.3 §2-§5).

EC-CMP-01, EC-CMP-03, EC-CMP-05, EC-CMP-06, EC-CMP-07, EC-CMP-08, EC-CMP-09, EC-CMP-18, EC-CMP-19.
"""

from datetime import timedelta

import pytest
from sqlalchemy import text

from tests.helpers import create_employee, invite_and_activate, signup_company
from tests.payroll.test_settings_engine import session, sql, today_of

PAY = "/api/v1/payroll"
COMPONENTS = f"{PAY}/components"
STRUCTURES = f"{PAY}/salary-structures"
SALARY_TAGS = ["LOP_APPLICABLE", "PART_OF_CTC", "PART_OF_GROSS"]
EXAMPLE_A = [
    {"component": "BASIC", "method": "FORMULA", "formula": "ctc.monthly * 50%"},
    {"component": "HRA", "method": "PERCENT_OF", "value": "40", "params": {"bases": ["BASIC"]}},
    {"component": "FIXED_ALLOWANCE", "method": "BALANCING"},
]
COMPENSATION_TABLES = (
    "pay_components", "pay_component_versions", "salary_templates", "salary_template_versions",
    "salary_template_lines", "compensation_assignments", "compensation_assignment_lines", "revision_cycles",
    "revision_cycle_items", "compensation_imports",
)


# --------------------------------------------------------------------------- shared helpers
def money(amount, currency="INR"):
    return {"amount": amount, "currency": currency}


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def seed(client):
    return ok(client.post(f"{COMPONENTS}/seed"))


def add_component(client, code, kind="EARNING", category="FIXED_ALLOWANCE", method="FIXED", **fields):
    body = {"code": code, "kind": kind, "category": category, "name": fields.pop("name", code.title()),
            "calc_method": method, **fields}
    return client.post(COMPONENTS, json=body)


def setup_catalogue(client):
    """Seeded components plus HRA (40% of BASIC by default), as in 3.3 §11 Example A."""
    seed(client)
    ok(add_component(client, "HRA", method="PERCENT_OF", params={"percent": "40", "bases": ["BASIC"]},
                     tags=SALARY_TAGS, proration="BY_PAID_DAYS"), 201)


def make_structure(client, code="STAFF_CTC", type_="CTC_BASED", lines=None, publish=True, **extra):
    body = {"code": code, "name": extra.pop("name", f"Structure {code}"), "type": type_,
            "lines": EXAMPLE_A if lines is None else lines,
            "test_range": extra.pop("test_range", {"min": "300000", "max": "5000000"}), **extra}
    created = ok(client.post(STRUCTURES, json=body), 201)
    if publish:
        created = ok(client.post(f"{STRUCTURES}/{created['id']}/publish", json={"version": 1}))
    return created


def active_version_id(structure):
    return next(v["id"] for v in structure["versions"] if v["status"] == "ACTIVE")


def payroll_user(app, admin, first, roles):
    return invite_and_activate(app, admin, create_employee(admin, first), roles)


@pytest.fixture
def acme(app):
    client = signup_company(app, "Acme Corp")
    setup_catalogue(client)
    return client


# --------------------------------------------------------------------------- catalogue
SEEDED_CODES = {"BASIC", "FIXED_ALLOWANCE", "BONUS", "COMMISSION", "INCENTIVE", "OT_WORKDAY", "OT_WEEKLY_OFF",
                "OT_HOLIDAY", "HOLIDAY_PREMIUM", "WEEKLY_OFF_PREMIUM", "SHIFT_ALLOWANCE", "TASK_EARNING", "ARREARS",
                "LEAVE_ENCASHMENT", "REIMBURSEMENT", "LOAN_RECOVERY", "ADVANCE_RECOVERY", "OTHER_DEDUCTION",
                "NET_ROUNDING"}


def test_seed_creates_the_country_neutral_set_once(app):
    """Signup provisions the catalogue; POST /components/seed is an idempotent top-up of missing codes."""
    client = signup_company(app, "Seedco")
    cid = client.company_id
    items = {c["code"]: c for c in ok(client.get(COMPONENTS))}
    assert set(items) == SEEDED_CODES  # exactly the country-neutral set, before anyone calls seed
    assert all(c["current_version"]["version"] == 1 and c["owner"] == "COMPANY" for c in items.values())
    seeded_audit = sql("SELECT details FROM audit_logs WHERE company_id = :c AND action = 'COMPONENTS_SEEDED'", c=cid)
    assert len(seeded_audit) == 1 and set(seeded_audit[0][0]["codes"]) == SEEDED_CODES  # recorded once, by the system
    assert seed(client)["created"] == []  # idempotent: nothing is missing
    assert {c["code"] for c in ok(client.get(COMPONENTS))} == SEEDED_CODES

    # A missing code (removed as the owner, unused) is topped up; the rest are left alone.
    bonus_before = items["BONUS"]["id"]
    sql("DELETE FROM pay_component_versions WHERE company_id = :c AND component_id = :p", c=cid, p=bonus_before)
    sql("DELETE FROM pay_components WHERE company_id = :c AND id = :p", c=cid, p=bonus_before)
    ids_before = dict(sql("SELECT code, id FROM pay_components WHERE company_id = :c", c=cid))
    assert seed(client)["created"] == ["BONUS"]
    after = {c["code"]: c for c in ok(client.get(COMPONENTS))}
    assert set(after) == SEEDED_CODES and after["BONUS"]["id"] != bonus_before
    assert after["BONUS"]["current_version"]["calc_method"] == "INPUT"
    assert {code: after[code]["id"] for code in ids_before} == ids_before  # existing components untouched
    assert seed(client)["created"] == []
    # An archived code still exists: the tenant's decision stands, seeding never brings it back.
    assert ok(client.patch(f"{COMPONENTS}/COMMISSION", json={"status": "ARCHIVED"}))["status"] == "ARCHIVED"
    assert seed(client)["created"] == []
    assert "COMMISSION" not in {c["code"] for c in ok(client.get(COMPONENTS))}
    assert sql("SELECT count(*) FROM pay_components WHERE company_id = :c AND code = 'COMMISSION'", c=cid) == [(1,)]
    items = after
    basic = items["BASIC"]["current_version"]
    assert set(basic["tags"]) == {"LOP_APPLICABLE", "OT_BASE", "ENCASHMENT_BASE", "PART_OF_CTC", "PART_OF_GROSS"}
    assert basic["totals_flags"] == {"in_gross": True, "in_net": True, "in_ctc": True, "in_employer_cost": True}
    assert items["FIXED_ALLOWANCE"]["current_version"]["calc_method"] == "BALANCING"
    assert items["OTHER_DEDUCTION"]["current_version"]["totals_flags"]["in_gross"] is False
    assert items["OT_WORKDAY"]["current_version"]["calc_method"] == "SYSTEM"  # no universal multiplier (K:157)
    assert ok(client.get(COMPONENTS, params={"kind": "DEDUCTION"})) and all(
        c["kind"] == "DEDUCTION" for c in ok(client.get(COMPONENTS, params={"kind": "DEDUCTION"})))


def test_component_create_get_and_versions_close_the_previous_one(acme):
    created = ok(add_component(acme, "SITE_ALLOWANCE", name="Site allowance", tags=["LOP_APPLICABLE"],
                               proration="BY_PAID_DAYS", proration_basis="FIXED_30",
                               frequency={"type": "SPECIFIC_MONTHS", "months": [3, 9]},
                               cap={"amount": "5000.00", "currency": "INR"}, visibility="HR_ONLY",
                               payslip={"group": "Allowances", "order": 40, "show_if_zero": False},
                               gl={"debit_account": "6100", "credit_account": "2100"},
                               input_approval_above=money("1000.00")), 201)
    version = created["current_version"]
    assert created["code"] == "SITE_ALLOWANCE" and version["version"] == 1 and version["proration_basis"] == "FIXED_30"
    assert version["frequency"] == {"type": "SPECIFIC_MONTHS", "months": [3, 9]}
    assert version["input_approval_above"] == {"amount": "1000.00", "currency": "INR"}
    cid = acme.company_id
    start = today_of(cid) + timedelta(days=10)
    r = acme.post(f"{COMPONENTS}/SITE_ALLOWANCE/versions",
                  json={"name": "Site allowance (revised)", "effective_from": start.isoformat()})
    detail = ok(r, 201)
    versions = {v["version"]: v for v in detail["versions"]}
    assert versions[1]["effective_to"] == (start - timedelta(days=1)).isoformat()
    assert versions[2]["name"] == "Site allowance (revised)" and versions[2]["cap"] == {"amount": "5000.00", "currency": "INR"}
    assert versions[2]["visibility"] == "HR_ONLY"  # unchanged properties carry over
    assert detail["current_version"]["version"] == 1  # the new version starts later
    past = today_of(cid) - timedelta(days=3)
    r = acme.post(f"{COMPONENTS}/SITE_ALLOWANCE/versions", json={"effective_from": past.isoformat()})
    assert r.status_code == 400 and r.json()["code"] == "REASON_REQUIRED"
    r = acme.post(f"{COMPONENTS}/SITE_ALLOWANCE/versions", json={"effective_from": past.isoformat(), "reason": "Fix"})
    versions = {v["version"]: v for v in ok(r, 201)["versions"]}
    assert versions[1]["status"] == "SUPERSEDED" and versions[3]["effective_to"] == versions[1]["effective_to"]
    actions = [a for (a,) in sql("SELECT action FROM audit_logs WHERE company_id = :c AND module = 'Compensation' "
                                 "AND entity_type = 'pay_component' ORDER BY id", c=cid)]
    assert actions[-3:] == ["COMPONENT_CREATED", "COMPONENT_VERSION_CREATED", "COMPONENT_VERSION_CREATED"]


def test_component_validation_rejects_bad_kinds_codes_and_totals(acme):
    assert add_component(acme, "HRA").json()["code"] == "COMPONENT_CODE_TAKEN"
    assert add_component(acme, "bad code").json()["code"] == "INVALID_CODE"
    assert add_component(acme, "MIN").json()["code"] == "INVALID_CODE"  # a function name is reserved
    assert add_component(acme, "X__Y").status_code == 400
    assert add_component(acme, "PF", kind="DEDUCTION", category="BASE").json()["code"] == "INVALID_CATEGORY"
    r = add_component(acme, "BIK", kind="BENEFIT_IN_KIND", category="BENEFIT", totals_flags={"in_net": True})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_TOTALS"  # a benefit in kind is never cash (EC-CMP-24)
    r = add_component(acme, "CASH", cap={"amount": 500, "currency": "INR"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_DECIMAL"  # JSON numbers are refused
    r = acme.post(COMPONENTS, json={"code": "Z", "kind": "EARNING", "category": "BASE", "name": "Z",
                                    "calc_method": "FIXED", "company_id": 99})
    assert r.status_code == 422  # strict request model
    assert add_component(acme, "BIK2", kind="BENEFIT_IN_KIND", category="BENEFIT").status_code == 201


def test_ec_cmp_05_two_components_may_share_a_display_name(acme):
    assert add_component(acme, "TRAVEL_A", name="Travel").status_code == 201
    assert add_component(acme, "TRAVEL_B", name="Travel").status_code == 201
    names = [c["code"] for c in ok(acme.get(COMPONENTS)) if c["name"] == "Travel"]
    assert sorted(names) == ["TRAVEL_A", "TRAVEL_B"]


def test_ec_cmp_06_code_is_immutable_once_used(acme):
    assert add_component(acme, "SPARE").status_code == 201
    renamed = ok(acme.patch(f"{COMPONENTS}/SPARE", json={"code": "SPARE_ALLOWANCE"}))
    assert renamed["code"] == "SPARE_ALLOWANCE"  # unused: may still be renamed
    make_structure(acme, lines=[*EXAMPLE_A, {"component": "SPARE_ALLOWANCE", "value": "100"}], publish=False)
    r = acme.patch(f"{COMPONENTS}/SPARE_ALLOWANCE", json={"code": "OTHER"})
    assert r.status_code == 409 and r.json()["code"] == "COMPONENT_CODE_IMMUTABLE"
    with pytest.raises(Exception, match="cannot change"):  # the database refuses it too
        sql("UPDATE pay_components SET code = 'SNEAKY' WHERE company_id = :c AND code = 'SPARE_ALLOWANCE'",
            c=acme.company_id)
    assert ok(acme.patch(f"{COMPONENTS}/SPARE_ALLOWANCE", json={"status": "ARCHIVED"}))["status"] == "ARCHIVED"
    assert "SPARE_ALLOWANCE" not in {c["code"] for c in ok(acme.get(COMPONENTS))}


def test_ec_cmp_18_pack_owned_components_are_read_only(acme):
    sql("INSERT INTO pay_components (company_id, code, kind, category, owner, status, created_at, updated_at) "
        "VALUES (:c, 'IN_EPF_EE', 'DEDUCTION', 'EMPLOYEE_CONTRIBUTION', 'PACK:IN', 'ACTIVE', now(), now())",
        c=acme.company_id)
    cid_component = sql("SELECT id FROM pay_components WHERE company_id = :c AND code = 'IN_EPF_EE'",
                        c=acme.company_id)[0][0]
    sql("INSERT INTO pay_component_versions (company_id, component_id, version, name, calc_method, effective_from, "
        "status, created_at, updated_at) VALUES (:c, :p, 1, 'EPF', 'SYSTEM', '2020-01-01', 'ACTIVE', now(), now())",
        c=acme.company_id, p=cid_component)
    detail = ok(acme.get(f"{COMPONENTS}/IN_EPF_EE"))
    assert detail["read_only"] is True and detail["owner"] == "PACK:IN"
    r = acme.post(f"{COMPONENTS}/IN_EPF_EE/versions", json={"name": "Hacked"})
    assert r.status_code == 403
    assert acme.patch(f"{COMPONENTS}/IN_EPF_EE", json={"status": "ARCHIVED"}).status_code == 403


# --------------------------------------------------------------------------- formulas
def test_ec_cmp_01_formula_cycle_is_rejected_with_its_path(acme):
    ok(add_component(acme, "ALPHA", method="FORMULA", formula="BASIC * 10%"), 201)
    ok(add_component(acme, "BETA", method="FORMULA", formula="ALPHA * 50%"), 201)
    r = acme.post(f"{COMPONENTS}/ALPHA/versions", json={"formula": "BETA + 100"})
    assert r.status_code == 400 and r.json()["code"] == "CYCLE_DETECTED"
    assert r.json()["detail"]["path"] == ["ALPHA", "BETA", "ALPHA"]
    check = ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": "BETA + 100", "component": "ALPHA"}))
    assert check["valid"] is False and check["errors"][0]["code"] == "CYCLE_DETECTED"


def test_ec_cmp_07_currency_mismatch_and_type_errors_block_saving(acme):
    sql("INSERT INTO payroll_tables (company_id, code, name, kind, currency, owner, effective_from, status, "
        "created_at, updated_at) VALUES (:c, 'BASIC_USD', 'USD basics', 'LOOKUP', 'USD', 'COMPANY', '2020-01-01', "
        "'ACTIVE', now(), now())", c=acme.company_id)
    r = add_component(acme, "USD_BASIC", method="FORMULA", formula='LOOKUP("BASIC_USD", emp.grade, 0)')
    assert r.status_code == 400 and r.json()["code"] == "CURRENCY_MISMATCH"
    r = add_component(acme, "BAD_TYPE", method="FORMULA", formula="BASIC + 5%")
    assert r.status_code == 400 and r.json()["code"] == "TYPE_MISMATCH"
    r = add_component(acme, "UNKNOWN", method="FORMULA", formula="NOPE * 2")
    assert r.status_code == 400 and r.json()["code"] == "UNKNOWN_REFERENCE"
    r = add_component(acme, "BAD_ELIG", eligibility="BASIC * 2", value=None)
    assert r.status_code in (400, 422)


def test_ec_cmp_08_ec_cmp_09_injection_and_limits_are_parse_errors(acme):
    for formula in ('__import__("os").system("x")', "BASIC; DROP TABLE employees", "<script>alert(1)</script>"):
        check = ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": formula}))
        assert check["valid"] is False and check["errors"][0]["code"] == "PARSE_ERROR", formula
        assert add_component(acme, "INJECT", method="FORMULA", formula=formula).status_code == 400
    deep = "(" * 60 + "1" + ")" * 60
    check = ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": deep}))
    assert check["errors"][0]["code"] == "LIMIT_EXCEEDED"
    long = " + ".join(["BASIC"] * 400)
    assert ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": long}))["errors"][0]["code"] == "LIMIT_EXCEEDED"
    assert acme.post(f"{COMPONENTS}/validate-formula", json={"formula": "x" * 4001}).status_code == 422


def test_ec_cmp_19_protected_attributes_and_ec_cmp_03_null_warnings(acme):
    r = add_component(acme, "GENDER_BONUS", method="FORMULA", formula='IF(emp.gender = "F", 100, 0)')
    assert r.status_code == 400 and r.json()["code"] == "PROTECTED_ATTRIBUTE"
    check = ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": "att.late_marks * 100"}))
    assert check["valid"] is True and any(w["code"] == "NULL_VALUE" for w in check["warnings"])
    created = ok(add_component(acme, "LATE_BONUS", method="FORMULA",
                               formula="IF(COALESCE(att.late_marks, 0) = 0, 500, 0)"), 201)
    assert created["warnings"] == []
    check = ok(acme.post(f"{COMPONENTS}/validate-formula", json={"formula": "att.present_days >= 20",
                                                                 "expect": "BOOLEAN"}))
    assert check["type"] == "BOOLEAN" and check["canonical"]


def test_formula_tester_with_samples_and_a_real_employee(app, acme):
    ok(add_component(acme, "ATT_BONUS", method="FORMULA", formula="IF(att.present_days >= 20, 1500, 0)"), 201)
    result = ok(acme.post(f"{COMPONENTS}/ATT_BONUS/test", json={"sample": {"variables": {"att.present_days": "22"}}}))
    assert result["ok"] is True and result["value"] == {"amount": "1500", "currency": "INR"}
    assert result["references"]["att.present_days"] == "22" and result["steps"]
    result = ok(acme.post(f"{COMPONENTS}/ATT_BONUS/test", json={"sample": {"variables": {"att.present_days": None}}}))
    assert result["ok"] is False and result["error"]["code"] == "NULL_VALUE"  # never a silent zero (EC-CMP-02)
    result = ok(acme.post(f"{COMPONENTS}/HRA/test", json={"formula": "DIVIDE(BASIC, att.paid_days, 0) + BASIC / 0",
                                                          "sample": {"components": {"BASIC": "50000"},
                                                                     "variables": {"att.paid_days": "30"}}}))
    assert result["ok"] is False and result["error"]["code"] == "DIVISION_BY_ZERO"

    from tests.payroll.test_compensation import assign_and_approve, people

    team = people(app, acme)
    structure = make_structure(acme)
    assign_and_approve(acme, team["approver"], team["worker"]["id"], active_version_id(structure), "1200000")
    result = ok(acme.post(f"{COMPONENTS}/HRA/test", json={"employee_id": team["worker"]["id"]}))
    assert result["value"]["amount"] == "20000" and result["source"]["type"] == "EMPLOYEE"
    # the tester for a real employee needs compensation access to that employee
    r = team["employee_client"].post(f"{COMPONENTS}/HRA/test", json={"employee_id": team["worker"]["id"]})
    assert r.status_code == 403


# --------------------------------------------------------------------------- authorization and isolation
def test_authorization_reading_needs_structure_read_and_writing_company_edit(app, acme):
    employee = payroll_user(app, acme, "Plain", ["employee"])
    assert employee.get(COMPONENTS).status_code == 403
    assert employee.post(f"{COMPONENTS}/validate-formula", json={"formula": "1"}).status_code == 403
    assert add_component(employee, "NOPE").status_code == 403
    approver = payroll_user(app, acme, "Appr", ["payroll_approver"])
    assert approver.get(COMPONENTS).status_code == 403  # approvers do not get structure read by default
    processor = payroll_user(app, acme, "Proc", ["payroll_processor"])
    assert ok(processor.get(COMPONENTS))
    assert add_component(processor, "PROC_ALLOWANCE").status_code == 201


def test_cross_tenant_components_are_not_found(app, acme):
    other = signup_company(app, "Beta Industries")
    seed(other)
    assert other.get(f"{COMPONENTS}/HRA").status_code == 404
    assert other.post(f"{COMPONENTS}/HRA/versions", json={"name": "x"}).status_code == 404
    assert other.post(f"{COMPONENTS}/HRA/test", json={}).status_code == 404
    assert "HRA" not in {c["code"] for c in ok(other.get(COMPONENTS))}


def test_three_tenant_isolation_of_every_compensation_table(app):
    """RLS on every new table: a tenant session sees only its own rows (EC-SEC-01)."""
    from tests.payroll.test_compensation import assign_and_approve, people

    companies = {}
    for name in ("Alpha Co", "Beta Co", "Gamma Co"):
        client = signup_company(app, name)
        setup_catalogue(client)
        team = people(app, client)
        ok(add_component(client, "SPECIAL", tags=SALARY_TAGS), 201)
        structure = make_structure(client, lines=[*EXAMPLE_A, {"component": "SPECIAL", "value": "1000",
                                                               "overridable": True}])
        assign_and_approve(client, team["approver"], team["worker"]["id"], active_version_id(structure), "1200000",
                           overrides={"SPECIAL": {"amount": "1500"}})
        cycle = ok(client.post(f"{PAY}/revision-cycles", json={
            "name": "Cycle", "method": {"type": "PERCENT", "percent": "5"},
            "effective_from": today_of(client.company_id).isoformat()}), 201)
        ok(client.post(f"{PAY}/revision-cycles/{cycle['id']}/preview"))
        ok(client.post(f"{PAY}/compensation/import", json={"mode": "dry_run", "csv": "employee_code,structure_code,"
                                                           "pay_basis_amount,effective_from\n"}))
        companies[name] = client.company_id
    for cid in companies.values():
        db = session(cid)
        try:
            for table in COMPENSATION_TABLES:
                owners = {row[0] for row in db.execute(text(f"SELECT DISTINCT company_id FROM {table}"))}
                assert owners == {cid}, table
        finally:
            db.close()
    with pytest.raises(Exception):
        db = session(companies["Alpha Co"])
        try:
            db.execute(text("INSERT INTO pay_components (company_id, code, kind, category, owner, status, created_at, "
                            "updated_at) VALUES (:c, 'X', 'EARNING', 'BASE', 'COMPANY', 'ACTIVE', now(), now())"),
                       {"c": companies["Beta Co"]})
            db.flush()
        finally:
            db.rollback()
            db.close()
