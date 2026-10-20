"""Legal entities, branches, placement history, entity/branch scopes, immutable payslips, currencies."""

import io

import pymupdf
import pytest

from tests.helpers import create_employee, invite_and_activate, role_id, signup_company
from tests.white_label_helpers import platform_admin, png, set_plan


@pytest.fixture
def acme(app):
    return signup_company(app, "Acme Corp")


def _entities(client):
    return {e["code"]: e for e in client.get("/api/v1/legal-entities").json()}


def _make_entity(client, code, name, country, currency, tz="UTC", **extra):
    r = client.post("/api/v1/legal-entities", json={
        "code": code, "legal_name": name, "country_code": country, "base_currency": currency, "timezone": tz, **extra,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _make_branch(client, entity_id, code, name, **extra):
    r = client.post("/api/v1/branches", json={"legal_entity_id": entity_id, "code": code, "name": name, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Structure and backfill
# ---------------------------------------------------------------------------
def test_signup_creates_default_entity_branch_and_placement(acme):
    entities = _entities(acme)
    assert set(entities) == {"LE01"} and entities["LE01"]["is_default"] and not entities["LE01"]["country_confirmed"]
    me = acme.me()
    history = acme.get(f"/api/v1/employees/{me['employee']['id']}/assignments").json()
    assert len(history) == 1 and history[0]["reason"] == "HIRE" and history[0]["is_current"]
    tree = acme.get("/api/v1/org/tree").json()
    assert tree["unconfirmed_entities"] == ["LE01"]


def test_rows_written_by_an_older_release_are_completed(app):
    """After a code rollback the previous release writes companies, employees and payroll without the
    new structure; the payroll trigger and org-sync's repair complete them."""
    from datetime import date
    from decimal import Decimal

    from app.database.db import SessionLocal, bind_tenant, system_session
    from app.models.branding import BrandProfile
    from app.models.company import Company
    from app.models.employee import Employee
    from app.models.org import Branch, EmployeeAssignment, LegalEntity
    from app.models.payroll import Payroll
    from app.services import company_service

    with system_session() as db:
        company = Company(name="Old Release Trading", slug="old-release-trading", currency="AED", timezone="Asia/Dubai")
        db.add(company)
        db.commit()
        company_id = company.id

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)

        def payroll(employee_id, month):
            # The previous release's model has no currency or legal_entity_id column.
            row = Payroll(company_id=company_id, employee_id=employee_id, salary_month=month,
                          basic_salary=Decimal("1000"), gross_salary=Decimal("1000"), net_salary=Decimal("1000"))
            db.add(row)
            db.flush()
            db.refresh(row)
            return row

        employee = Employee(company_id=company_id, employee_code="OLD-1", first_name="Layla", last_name="Haddad",
                            email="layla@old-release.example", hire_date=date(2025, 1, 6), location="Dubai Marina")
        db.add(employee)
        db.flush()
        before = payroll(employee.id, date(2026, 8, 1))
        assert (before.currency, before.legal_entity_id) == ("AED", None)  # company currency, not a static INR

        assert company_service.repair_defaults(db, db.get(Company, company_id)) == {
            "entities": 1, "branches": 2, "placements": 1, "brands": 1}
        entity = db.query(LegalEntity).filter(LegalEntity.company_id == company_id).one()
        assert (entity.code, entity.base_currency, entity.country_code, entity.country_confirmed) == ("LE01", "AED", "AE", False)
        assert sorted(b.name for b in db.query(Branch).filter(Branch.company_id == company_id)) == ["Dubai Marina", "Main"]
        placement = db.query(EmployeeAssignment).filter(EmployeeAssignment.employee_id == employee.id).one()
        assert (placement.reason, placement.effective_from, placement.effective_to) == ("MIGRATION", date(2025, 1, 6), None)
        assert db.query(BrandProfile).filter(BrandProfile.company_id == company_id, BrandProfile.status == "PUBLISHED").count() == 1

        after = payroll(employee.id, date(2026, 9, 1))
        assert (after.currency, after.legal_entity_id) == ("AED", entity.id)
        assert company_service.repair_defaults(db, db.get(Company, company_id)) == {
            "entities": 0, "branches": 0, "placements": 0, "brands": 0}
        db.commit()
    finally:
        db.close()


def test_hire_with_manager_keeps_branch_location_for_regional_holidays(app, acme):
    blr = acme.get("/api/v1/branches").json()[0]
    delhi = acme.post("/api/v1/branches", json={"legal_entity_id": blr["legal_entity_id"], "code": "DEL", "name": "Delhi"}).json()
    boss = create_employee(acme, "Boss", branch_id=blr["id"])
    report = create_employee(acme, "Report", branch_id=delhi["id"], manager_id=boss["id"])
    assert acme.get(f"/api/v1/employees/{report['id']}").json()["location"] == "Delhi"
    acme.post("/api/v1/holidays", json={"name": "Delhi Day", "date": "2026-11-11", "location": "Delhi"})
    acme.post("/api/v1/holidays", json={"name": "Company Day", "date": "2026-11-12"})
    user = invite_and_activate(app, acme, report, ["employee"])
    mine = [h["name"] for h in user.get("/api/v1/holidays", params={"year": 2026, "mine": True}).json()]
    assert mine == ["Delhi Day", "Company Day"]


def test_free_text_location_becomes_a_branch_and_moves_with_the_employee(app, acme):
    boss = create_employee(acme, "Lead")
    hired = create_employee(acme, "Pune", location="Pune", manager_id=boss["id"])
    assert acme.get(f"/api/v1/employees/{hired['id']}").json()["location"] == "Pune"
    branches = {b["name"]: b for b in acme.get("/api/v1/branches").json()}
    assert "Pune" in branches
    history = acme.get(f"/api/v1/employees/{hired['id']}/assignments").json()
    assert [h["branch"] for h in history if h["is_current"]] == ["Pune"]
    # Editing the location on the employee form is a dated transfer, not an overwrite.
    r = acme.put(f"/api/v1/employees/{hired['id']}", json={"location": "Mumbai"})
    assert r.status_code == 200, r.text
    assert acme.get(f"/api/v1/employees/{hired['id']}").json()["location"] == "Mumbai"
    history = acme.get(f"/api/v1/employees/{hired['id']}/assignments").json()
    assert len(history) == 2 and [(h["reason"], h["branch"]) for h in history if h["is_current"]] == [("TRANSFER", "Mumbai")]
    acme.post("/api/v1/holidays", json={"name": "Mumbai Day", "date": "2026-11-13", "location": "Mumbai"})
    user = invite_and_activate(app, acme, hired, ["employee"])
    assert "Mumbai Day" in [h["name"] for h in user.get("/api/v1/holidays", params={"year": 2026, "mine": True}).json()]
    # The daily job keeps it that way.
    from app.cli import org_sync

    org_sync()
    assert acme.get(f"/api/v1/employees/{hired['id']}").json()["location"] == "Mumbai"


def test_entity_validation_iso_codes_and_country_lock(app, acme):
    for bad in ({"country_code": "XX"}, {"base_currency": "ABC"}, {"timezone": "Mars/Base"}):
        payload = {"code": "BAD", "legal_name": "Bad", "country_code": "IN", "base_currency": "INR", "timezone": "UTC", **bad}
        assert acme.post("/api/v1/legal-entities", json=payload).status_code == 400
    ae = _make_entity(acme, "AE01", "Acme Gulf FZ-LLC", "AE", "AED", "Asia/Dubai",
                      registered_address={"line1": "Office 4", "city": "Dubai", "subdivision_code": "AE-DU"},
                      registration_lines=[{"label": "Trade licence", "value": "DMCC-123"}])
    assert ae["registered_address"]["subdivision_code"] == "AE-DU" and ae["capability"] == "CONFIGURATION_ONLY"
    assert acme.post("/api/v1/legal-entities", json={"code": "AE01", "legal_name": "Dup", "country_code": "AE",
                                                     "base_currency": "AED", "timezone": "UTC"}).status_code == 409
    # Branch subdivision must belong to the entity's country.
    assert acme.post("/api/v1/branches", json={"legal_entity_id": ae["id"], "code": "X", "name": "Wrong",
                                               "subdivision_code": "IN-MH"}).status_code == 400
    dubai = _make_branch(acme, ae["id"], "DXB", "Dubai Office", subdivision_code="AE-DU")
    assert dubai["timezone"] == "Asia/Dubai" and dubai["country_code"] == "AE"
    # Confirm the default entity's country.
    le01 = _entities(acme)["LE01"]
    r = acme.patch(f"/api/v1/legal-entities/{le01['id']}", json={"country_code": "IN", "confirm_country": True})
    assert r.status_code == 200 and r.json()["country_confirmed"]


def test_department_cycle_rejected_and_teams_extend_department_scope(app, acme):
    eng = acme.post("/api/v1/departments", json={"name": "Engineering"}).json()
    team = acme.post("/api/v1/departments", json={"name": "Platform team", "parent_department_id": eng["id"]}).json()
    r = acme.put(f"/api/v1/departments/{eng['id']}", json={"name": "Engineering", "parent_department_id": team["id"]})
    assert r.status_code == 400 and r.json()["code"] == "ORG_DEPARTMENT_CYCLE"
    r = acme.put(f"/api/v1/departments/{eng['id']}", json={"name": "Engineering", "parent_department_id": eng["id"]})
    assert r.status_code == 400

    head = create_employee(acme, "Head", department_id=eng["id"])
    member = create_employee(acme, "TeamMember", department_id=team["id"])
    outsider = create_employee(acme, "Outsider")
    acme.put(f"/api/v1/departments/{eng['id']}", json={"name": "Engineering", "head_employee_id": head["id"]})
    role = acme.post("/api/v1/roles", json={"name": "Dept viewer", "grants": [{"permission": "employee:read", "scope": "DEPARTMENT"}]})
    assert role.status_code == 200, role.text
    viewer = invite_and_activate(app, acme, head, [])
    acme.put(f"/api/v1/users/{viewer.user_id}/roles", json={"role_ids": [role.json()["id"]]})
    ids = {e["id"] for e in viewer.get("/api/v1/employees?limit=200").json()["items"]}
    assert member["id"] in ids and outsider["id"] not in ids


def test_placement_change_history_and_overlap_rules(app, acme):
    le01 = _entities(acme)["LE01"]
    main = acme.get("/api/v1/branches").json()[0]
    pune = _make_branch(acme, le01["id"], "PNQ", "Pune")
    ae = _make_entity(acme, "AE01", "Acme Gulf FZ-LLC", "AE", "AED", "Asia/Dubai")
    dubai = _make_branch(acme, ae["id"], "DXB", "Dubai")
    emp = create_employee(acme, "Mover", hire_date="2024-01-15")
    base = f"/api/v1/employees/{emp['id']}/assignments"
    r = acme.post(base, json={"branch_id": pune["id"], "effective_from": "2024-06-01", "reason": "TRANSFER"})
    assert r.status_code == 200, r.text
    history = r.json()
    assert [h["branch"] for h in history] == ["Pune", main["name"]]
    assert history[1]["effective_to"] == "2024-05-31"
    assert acme.get(f"/api/v1/employees/{emp['id']}").json()["location"] == "Pune"  # legacy column synced
    # Cannot start before the current placement; concurrent assignments disabled.
    assert acme.post(base, json={"branch_id": dubai["id"], "effective_from": "2024-05-01"}).status_code == 409
    assert acme.post(base, json={"branch_id": dubai["id"], "effective_from": "2024-07-01", "is_primary": False}).status_code == 400
    # Branch must belong to the chosen entity.
    r = acme.post(base, json={"branch_id": dubai["id"], "legal_entity_id": le01["id"], "effective_from": "2024-07-01"})
    assert r.status_code == 400
    r = acme.post(base, json={"branch_id": dubai["id"], "effective_from": "2024-07-01", "reason": "TRANSFER"})
    assert r.status_code == 200 and r.json()[0]["currency"] == "AED"

    # The database itself refuses overlapping primary placements.
    from sqlalchemy import text

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        with pytest.raises(Exception) as err:
            conn.execute(text(
                "INSERT INTO employee_assignments (company_id, employee_id, legal_entity_id, branch_id, is_primary, reason, "
                "effective_from, created_at, updated_at) VALUES (:c, :e, :le, :b, true, 'CORRECTION', '2024-08-01', now(), now())"
            ), {"c": acme.company_id, "e": emp["id"], "le": ae["id"], "b": dubai["id"]})
        assert "overlap" in str(err.value)


def test_termination_and_rehire_keep_history(acme):
    emp = create_employee(acme, "Leaver", hire_date="2024-01-15")
    assert acme.post(f"/api/v1/employees/{emp['id']}/deactivate").status_code == 200
    closed = acme.get(f"/api/v1/employees/{emp['id']}/assignments").json()
    assert closed[0]["effective_to"] is not None
    assert acme.post(f"/api/v1/employees/{emp['id']}/reactivate").status_code == 200
    history = acme.get(f"/api/v1/employees/{emp['id']}/assignments").json()
    assert [h["reason"] for h in history] == ["REHIRE", "HIRE"]
    assert acme.get(f"/api/v1/employees/{emp['id']}").json()["employee_code"] == emp["employee_code"]


# ---------------------------------------------------------------------------
# LEGAL_ENTITY and BRANCH scopes (EC-ORG-12, EC-ORG-13)
# ---------------------------------------------------------------------------
def test_entity_and_branch_scopes_need_targets_and_never_cross_tenants(app, acme):
    other = signup_company(app, "Other Co")
    other_branch = other.get("/api/v1/branches").json()[0]
    le01 = _entities(acme)["LE01"]
    ae = _make_entity(acme, "AE01", "Acme Gulf FZ-LLC", "AE", "AED", "Asia/Dubai")
    dubai = _make_branch(acme, ae["id"], "DXB", "Dubai")
    india_emp = create_employee(acme, "Indian")
    gulf_emp = create_employee(acme, "Gulfie")
    acme.post(f"/api/v1/employees/{gulf_emp['id']}/assignments",
              json={"branch_id": dubai["id"], "effective_from": "2024-02-01"})
    role = acme.post("/api/v1/roles", json={"name": "Gulf HR", "grants": [
        {"permission": "employee:read", "scope": "LEGAL_ENTITY"}, {"permission": "attendance:read", "scope": "BRANCH"},
        {"permission": "leave:read", "scope": "LEGAL_ENTITY"}]})
    assert role.status_code == 200, role.text
    gulf_hr_emp = create_employee(acme, "GulfHR")
    gulf_hr = invite_and_activate(app, acme, gulf_hr_emp, [])
    acme.put(f"/api/v1/users/{gulf_hr.user_id}/roles", json={"role_ids": [role.json()["id"]]})

    # No targets: deny by default.
    assert gulf_hr.get("/api/v1/employees?limit=200").json()["items"] == []
    assert gulf_hr.get(f"/api/v1/employees/{gulf_emp['id']}").status_code == 404

    targets = gulf_hr.get(f"/api/v1/users/{gulf_hr.user_id}/scope-targets")
    assert targets.status_code == 403  # needs user:read
    r = acme.put(f"/api/v1/users/{gulf_hr.user_id}/scope-targets", json={"assignments": [
        {"role_id": role.json()["id"], "legal_entity_ids": [ae["id"]], "branch_ids": [other_branch["id"]]}]})
    assert r.status_code == 404  # another tenant's branch id
    r = acme.put(f"/api/v1/users/{gulf_hr.user_id}/scope-targets", json={"assignments": [
        {"role_id": role.json()["id"], "legal_entity_ids": [ae["id"]], "branch_ids": [dubai["id"]]}]})
    assert r.status_code == 200, r.text
    ids = {e["id"] for e in gulf_hr.get("/api/v1/employees?limit=200").json()["items"]}
    assert ids == {gulf_emp["id"]}
    assert gulf_hr.get(f"/api/v1/employees/{india_emp['id']}").status_code == 404
    # Managers/HR at an entity scope get no payroll.
    assert gulf_hr.get("/api/v1/payroll").status_code == 403
    # After a transfer out of the entity the employee leaves the scope.
    acme.post(f"/api/v1/employees/{gulf_emp['id']}/assignments",
              json={"branch_id": acme.get("/api/v1/branches?legal_entity_id=%d" % le01["id"]).json()[0]["id"],
                    "effective_from": "2024-03-01"})
    assert gulf_hr.get(f"/api/v1/employees/{gulf_emp['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Immutable, branded payslips (EC-WL-09, EC-DOC-23) and currencies
# ---------------------------------------------------------------------------
def _payroll_people(app, acme):
    preparer = invite_and_activate(app, acme, create_employee(acme, "Prep"), ["payroll_manager"])
    approver = invite_and_activate(app, acme, create_employee(acme, "Appr"), ["payroll_manager"])
    return preparer, approver


def _pdf_text(data: bytes) -> str:
    return "".join(page.get_text() for page in pymupdf.open(stream=data, filetype="pdf"))


def test_issued_payslip_never_changes_after_brand_change(app, acme):
    ops = platform_admin(app)
    set_plan(ops, acme.company_id, "enterprise")
    preparer, approver = _payroll_people(app, acme)
    worker = create_employee(acme, "Priya", "शर्मा")
    acme.post("/api/v1/salary-structures", json={"employee_id": worker["id"], "basic_salary": "123456.78",
                                                  "effective_from": "2024-01-01"})
    le01 = _entities(acme)["LE01"]
    acme.patch(f"/api/v1/legal-entities/{le01['id']}", json={
        "legal_name": "Acme India Private Limited", "country_code": "IN",
        "registration_lines": [{"label": "CIN", "value": "U72200KA2019PTC123456"}]})
    acme.put("/api/v1/branding/draft", json={"changes": {"primary_color": "#0F766E", "legal_footer": "Old footer"}})
    acme.post("/api/v1/branding/publish", json={})

    pid = preparer.post(f"/api/v1/payroll/generate?employee_id={worker['id']}&year=2025&month=3").json()["id"]
    draft_pdf = acme.get(f"/api/v1/payroll/{pid}/slip.pdf")
    assert "DRAFT" in _pdf_text(draft_pdf.content)
    assert approver.post(f"/api/v1/payroll/{pid}/approve").status_code == 200
    first = acme.get(f"/api/v1/payroll/{pid}/slip.pdf")
    text = _pdf_text(first.content)
    assert "₹" in text and "■" not in text  # rupee glyph renders (EC-DOC-23)
    assert "Acme India Private Limited" in text and "U72200KA2019PTC123456" in text and "Old footer" in text
    assert "श" in text and "Priya" in text  # Devanagari name, shaped with HarfBuzz (conjuncts map to glyph ids)
    original_sha = first.headers["x-document-sha256"]

    # Rebrand: new logo, colour and footer.
    logo = acme.post("/api/v1/branding/assets", data={"kind": "logo_light"},
                     files={"file": ("l.png", png(500, 140, (220, 38, 38, 255)), "image/png")}).json()
    acme.put("/api/v1/branding/draft", json={"changes": {"logo_light": logo["id"], "primary_color": "#DC2626",
                                                         "legal_footer": "New footer"}})
    acme.post("/api/v1/branding/publish", json={})
    reprint = acme.get(f"/api/v1/payroll/{pid}/slip.pdf")
    assert reprint.content == first.content and reprint.headers["x-document-sha256"] == original_sha
    assert "Old footer" in _pdf_text(reprint.content)

    # A payslip issued now uses the new brand.
    pid2 = preparer.post(f"/api/v1/payroll/generate?employee_id={worker['id']}&year=2025&month=4").json()["id"]
    approver.post(f"/api/v1/payroll/{pid2}/approve")
    assert "New footer" in _pdf_text(acme.get(f"/api/v1/payroll/{pid2}/slip.pdf").content)

    # Even if the stored file disappears, the reprint comes from the frozen snapshot.
    from app.core import storage
    from app.database.db import system_session
    from app.models.branding import IssuedDocument

    with system_session() as db:
        doc = db.query(IssuedDocument).filter(IssuedDocument.source_id == pid).one()
        storage.resolve(acme.company_id, doc.storage_path).unlink()
    rebuilt = acme.get(f"/api/v1/payroll/{pid}/slip.pdf")
    assert rebuilt.status_code == 200 and "Old footer" in _pdf_text(rebuilt.content)

    # The app role cannot rewrite or delete an issued document.
    from sqlalchemy import text as sql

    from app.database.db import engine

    with engine.begin() as conn:
        conn.execute(sql("SELECT set_config('app.company_id', :c, true)"), {"c": str(acme.company_id)})
        with pytest.raises(Exception):
            conn.execute(sql("UPDATE issued_documents SET snapshot = '{}'::jsonb"))
    with engine.begin() as conn:
        conn.execute(sql("SELECT set_config('app.company_id', :c, true)"), {"c": str(acme.company_id)})
        with pytest.raises(Exception):
            conn.execute(sql("DELETE FROM issued_documents"))


def test_entity_document_branding_requires_enterprise(app, acme):
    le01 = _entities(acme)["LE01"]
    r = acme.put(f"/api/v1/legal-entities/{le01['id']}/document-brand", json={"changes": {"accent_color": "#1D4ED8"}})
    assert r.status_code == 403
    ops = platform_admin(app)
    set_plan(ops, acme.company_id, "enterprise")
    r = acme.put(f"/api/v1/legal-entities/{le01['id']}/document-brand", json={"changes": {"accent_color": "#1D4ED8", "legal_footer": "Entity footer"}})
    assert r.status_code == 200, r.text
    assert acme.post(f"/api/v1/legal-entities/{le01['id']}/document-brand/publish", json={}).status_code == 200
    effective = acme.get(f"/api/v1/legal-entities/{le01['id']}/document-brand").json()["effective"]
    assert effective["accent_strip"] == "#1D4ED8" and effective["legal_footer"] == "Entity footer"
    assert acme.put(f"/api/v1/legal-entities/{le01['id']}/document-brand", json={"changes": {"product_name": "x"}}).status_code == 400


def test_country_change_blocked_after_approved_payroll_and_mixed_currency_totals(app, acme):
    preparer, approver = _payroll_people(app, acme)
    le01 = _entities(acme)["LE01"]
    ae = _make_entity(acme, "AE01", "Acme Gulf FZ-LLC", "AE", "AED", "Asia/Dubai")
    dubai = _make_branch(acme, ae["id"], "DXB", "Dubai")
    india = create_employee(acme, "Inder")
    gulf = create_employee(acme, "Fatima", "Zahra", first_name="فاطمة", last_name="الزهراء")
    acme.post(f"/api/v1/employees/{gulf['id']}/assignments", json={"branch_id": dubai["id"], "effective_from": "2024-02-01"})
    for emp, amount in ((india, "50000"), (gulf, "12000")):
        acme.post("/api/v1/salary-structures", json={"employee_id": emp["id"], "basic_salary": amount, "effective_from": "2024-01-01"})
    p1 = preparer.post(f"/api/v1/payroll/generate?employee_id={india['id']}&year=2025&month=6").json()
    p2 = preparer.post(f"/api/v1/payroll/generate?employee_id={gulf['id']}&year=2025&month=6").json()
    assert (p1["currency"], p2["currency"]) == ("INR", "AED")
    summary = acme.get("/api/v1/payroll/summary/2025/6").json()
    assert summary["mixed_currency"] and summary["total_net_salary"] is None
    assert set(summary["by_currency"]) == {"INR", "AED"}
    report = acme.get("/api/v1/reports/payroll?year=2025&month=6").json()
    assert report["currency"] is None and "total_net_AED" in report["summary"] and "total_net" not in report["summary"]

    approver.post(f"/api/v1/payroll/{p2['id']}/approve")
    pdf = acme.get(f"/api/v1/payroll/{p2['id']}/slip.pdf").content
    text = _pdf_text(pdf)
    assert "AED 12,000.00" in text and "Acme Gulf FZ-LLC" in text
    # Arabic is shaped (joined contextual forms), not printed as isolated letters.
    assert any("ﹰ" <= ch <= "﻾" for ch in text)
    r = acme.patch(f"/api/v1/legal-entities/{ae['id']}", json={"country_code": "SA"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_COUNTRY_LOCKED"
    # India entity has no approved payroll yet, so its country may still be corrected.
    assert acme.patch(f"/api/v1/legal-entities/{le01['id']}", json={"country_code": "IN"}).status_code == 200
    # A transfer into an approved period is refused.
    main = next(b for b in acme.get("/api/v1/branches").json() if b["name"] == "Main")
    r = acme.post(f"/api/v1/employees/{gulf['id']}/assignments", json={"branch_id": main["id"], "effective_from": "2025-06-15"})
    assert r.status_code == 409 and r.json()["code"] == "ORG_PERIOD_FINALIZED"
