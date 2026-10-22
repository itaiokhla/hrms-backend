"""Compliance page and statutory liabilities (3.8 §5.15, 3.2 §6.2, §8.1, §8.3; decisions I-07, I-15).

Per legal entity: country, capability badge (production / fixture / not verified; a recorded platform
level restricts but never upgrades), pack versions and review dates, attestation status, registrations,
liabilities with remittance status per currency and the due-date calendar of a usable pack. Liabilities move
OPEN → REMITTED (reference required) → RECONCILED through one transition with a row lock and row version.

EC-STA-10 (employee and employer portions separately), EC-MON-04 (per currency), EC-SEC-01/02/05.
"""

from datetime import date

from tests.payroll.test_reports import ok, other_tenant, rc_world  # noqa: F401  (rc_world is a fixture)
from tests.payroll.test_wage_inputs import audit_actions, session, sql

PAY = "/api/v1/payroll"
COMPLIANCE = f"{PAY}/compliance"
LIABILITIES = f"{PAY}/liabilities"


def cards(client, **params):
    data = ok(client.get(COMPLIANCE, params=params))
    return {c["legal_entity"]["code"]: c for c in data["entities"]}, data


def liability_id(company_id, scheme):
    return sql("SELECT id FROM statutory_liabilities WHERE company_id = :c AND scheme = :s ORDER BY id LIMIT 1",
               c=company_id, s=scheme)[0][0]


def test_compliance_page_shows_capability_packs_attestation_registrations_and_due_dates(rc_world):
    w = rc_world
    sql("UPDATE legal_entities SET registration_lines = CAST(:r AS jsonb) WHERE id = :e",
        r='[{"label": "Pension employer no.", "value": "ZP-123"}]', e=w["zz01"])
    by_code, data = cards(w["acme"], on="2026-09-30")
    assert data["as_of"] == "2026-09-30" and data["environment"] == "test"
    assert list(by_code) == ["LE01", "AE01", "ZZ01"]

    zz = by_code["ZZ01"]
    assert zz["country"]["code"] == "ZZ"
    assert (zz["capability"]["kind"], zz["capability"]["level"]) == ("PRODUCTION", "VERIFIED")
    assert zz["active_pack"]["version"] == "ZZ-2026.1" and zz["active_pack"]["reviewer"] == "Test fixture reviewer"
    assert zz["active_pack"]["reviewed_on"] == "2026-01-01" and zz["active_pack"]["watermark"] is None
    assert zz["attestation"]["status"] == "NOT_REQUIRED" and zz["attestation"]["blocks_finalization"] is False
    assert zz["registrations"] == [{"label": "Pension employer no.", "value": "ZP-123"}]
    assert [r["code"] for r in zz["report_definitions"]] == ["ZZ_PENSION_RETURN", "ZZ_WHT_RETURN"]
    assert zz["liabilities"]["per_currency"]["EUR"]["open"] == "1340.00"
    assert zz["liabilities"]["open_count"] == 2 and zz["liabilities"]["next_due_date"] == "2026-10-10"
    calendar = zz["due_calendar"]
    assert calendar["available"] is True and calendar["pack_version"] == "ZZ-2026.1"
    pension = [i for i in calendar["items"] if i["kind"] == "CONTRIBUTION" and i["scheme"] == "ZZ.pension"
               and i["period_end"] == "2026-09-30"]
    assert [(i["due_date"], i["status"], i["components"]) for i in pension] == [
        ("2026-10-15", "OPEN", ["ZZ_PENSION_EE", "ZZ_PENSION_ER"])]
    returns = [i for i in calendar["items"] if i["kind"] == "REPORT" and i["period_end"] == "2026-09-30"]
    assert {(i["report_code"], i["due_date"], i["status"]) for i in returns} == {
        ("ZZ_PENSION_RETURN", "2026-10-15", "NOT_STARTED"), ("ZZ_WHT_RETURN", "2026-10-10", "NOT_STARTED")}

    india = by_code["LE01"]  # the IN fixture pack: usable in test, badged "FIXTURE — not law"
    assert india["capability"]["kind"] == "FIXTURE" and india["capability"]["level"] == "CALC_SUPPORTED"
    assert india["active_pack"]["is_fixture"] is True and india["active_pack"]["watermark"] == "FIXTURE — not law"
    assert india["attestation"]["status"] == "NOT_REQUIRED"
    assert india["liabilities"]["per_currency"]["INR"]["open"] == "7100.00"

    gulf = by_code["AE01"]  # a DRAFT pack: no statutory output, attestation needed, no due dates
    assert gulf["capability"]["kind"] == "NOT_VERIFIED" and gulf["capability"]["level"] == "CONFIG_ONLY"
    assert gulf["attestation"] == {**gulf["attestation"], "required": True, "status": "MISSING",
                                   "blocks_finalization": True}
    assert gulf["due_calendar"]["available"] is False and gulf["due_calendar"]["items"] == []
    assert gulf["report_definitions"] == []
    assert any(p["status"] == "DRAFT" for p in gulf["pack_versions"])


def test_recorded_platform_capability_restricts_but_never_upgrades(rc_world):
    w = rc_world
    sql("INSERT INTO country_capabilities (alpha2, capability_level, note, updated_by, updated_at) "
        "VALUES ('ZZ', 'CONFIG_ONLY', 'Paused by the platform', 'test', now()), "
        "('AE', 'VERIFIED', 'Claims more than the packs support', 'test', now())")
    by_code, _ = cards(w["acme"], on="2026-09-30")
    zz = by_code["ZZ01"]["capability"]
    assert (zz["level"], zz["kind"], zz["recorded"], zz["from_packs"]) == (
        "CONFIG_ONLY", "NOT_VERIFIED", "CONFIG_ONLY", "VERIFIED")
    assert zz["recorded_note"] == "Paused by the platform"
    ae = by_code["AE01"]["capability"]
    assert (ae["level"], ae["kind"]) == ("CONFIG_ONLY", "NOT_VERIFIED")


def test_attestation_status_pending_and_active_for_entities_without_a_verified_pack(app, rc_world):
    from app.models.pay_runs import StatutoryAttestation
    from tests.payroll.test_reports import with_roles

    w = rc_world
    founder = sql("SELECT id FROM users WHERE email = :m", m=w["acme"].email)[0][0]
    checker = with_roles(app, w["acme"], "Chuck", ["payroll_manager"])
    with session(w["cid"]) as (db, _):
        row = StatutoryAttestation(company_id=w["cid"], legal_entity_id=w["ae01"], schemes=[], reason="outside",
                                   effective_from=date(2026, 1, 1), status="PENDING", attested_by_user_id=founder,
                                   row_version=1)
        db.add(row)
        db.commit()
        attestation = row.id
    gulf = cards(w["acme"], on="2026-09-30")[0]["AE01"]["attestation"]
    assert gulf["status"] == "PENDING_APPROVAL" and gulf["pending"]["id"] == attestation
    assert gulf["blocks_finalization"] is True
    sql("UPDATE statutory_attestations SET status = 'ACTIVE', approved_by_user_id = :u, approved_at = now() "
        "WHERE id = :a", u=checker.user_id, a=attestation)
    gulf = cards(w["acme"], on="2026-09-30")[0]["AE01"]["attestation"]
    assert gulf["status"] == "ACTIVE" and gulf["blocks_finalization"] is False
    assert gulf["active"]["approved_by_user_id"] == checker.user_id
    assert cards(w["acme"], on="2025-12-31")[0]["AE01"]["attestation"]["status"] == "MISSING"


def test_liabilities_per_currency_and_the_remittance_lifecycle(rc_world):
    w = rc_world
    acme = w["acme"]
    data = ok(acme.get(LIABILITIES))
    assert data["total"] == 3
    assert data["totals"] == {"EUR": {"OPEN": "1340.00"}, "INR": {"OPEN": "7100.00"}}
    pension = next(i for i in data["items"] if i["scheme"] == "ZZ.pension")
    assert pension["employee_amount"] == {"amount": "350.00", "currency": "EUR"}  # EC-STA-10
    assert pension["employer_amount"] == {"amount": "490.00", "currency": "EUR"}
    assert pension["total"] == {"amount": "840.00", "currency": "EUR"} and pension["status"] == "OPEN"
    india = next(i for i in data["items"] if i["scheme"] == "IN.epf")
    assert india["fixture"] is True and india["watermark"] == "FIXTURE — not law"
    assert ok(acme.get(LIABILITIES, params={"currency": "EUR", "scheme": "ZZ.wht"}))["total"] == 1
    assert ok(acme.get(LIABILITIES, params={"legal_entity_id": w["le01"]}))["total"] == 1

    lid = pension["id"]
    assert acme.post(f"{LIABILITIES}/{lid}/remit", json={}).status_code == 422
    assert acme.post(f"{LIABILITIES}/{lid}/remit", json={"remittance_ref": "   "}).status_code == 422
    assert acme.post(f"{LIABILITIES}/{lid}/remit", json={"remittance_ref": "CH-1", "remitted_on": "2099-01-01"}
                     ).json()["code"] == "REMITTANCE_DATE_IN_FUTURE"
    assert acme.post(f"{LIABILITIES}/{lid}/reconcile", json={}).json()["code"] == "INVALID_TRANSITION"
    assert acme.post(f"{LIABILITIES}/{lid}/remit", json={"remittance_ref": "CH-1", "row_version": 9}
                     ).json()["code"] == "ROW_VERSION_CONFLICT"
    remitted = ok(acme.post(f"{LIABILITIES}/{lid}/remit", json={
        "remittance_ref": "CH-2026-10-0042", "remitted_on": "2026-10-12", "note": "Paid via bank portal",
        "row_version": 1}))
    assert remitted == {"id": lid, "status": "REMITTED", "remittance_ref": "CH-2026-10-0042", "row_version": 2}
    detail = ok(acme.get(f"{LIABILITIES}/{lid}"))
    assert detail["remitted_on"] == "2026-10-12" and detail["overdue"] is False
    assert ok(acme.post(f"{LIABILITIES}/{lid}/reconcile", json={}))["status"] == "RECONCILED"
    assert acme.post(f"{LIABILITIES}/{lid}/reconcile", json={}).status_code == 409
    assert audit_actions(w["cid"], "statutory_liability", lid) == ["LIABILITY_REMITTED", "LIABILITY_RECONCILED"]
    totals = ok(acme.get(LIABILITIES))["totals"]
    assert totals["EUR"] == {"OPEN": "500.00", "RECONCILED": "840.00"}

    # Overdue: an OPEN liability whose due date has passed.
    sql("UPDATE statutory_liabilities SET due_date = '2020-01-10' WHERE company_id = :c AND scheme = 'ZZ.wht'",
        c=w["cid"])
    overdue = ok(acme.get(LIABILITIES, params={"overdue": "true", "currency": "EUR"}))
    assert [i["scheme"] for i in overdue["items"]] == ["ZZ.wht"] and overdue["items"][0]["overdue"] is True
    zz = cards(acme)[0]["ZZ01"]["liabilities"]
    assert zz["overdue_count"] == 1 and zz["per_currency"]["EUR"]["overdue"] == "500.00"


def test_ec_sec_05_scope_and_permissions_of_compliance_and_liabilities(app, rc_world):
    from tests.payroll.test_reports import with_roles

    w = rc_world
    cleo = w["cleo"]  # statutory review + reports at ZZ01
    by_code, _ = cards(cleo)
    assert list(by_code) == ["ZZ01"]
    assert cleo.get(COMPLIANCE, params={"legal_entity_id": w["le01"]}).status_code == 404
    data = ok(cleo.get(LIABILITIES))
    assert {i["scheme"] for i in data["items"]} == {"ZZ.pension", "ZZ.wht"} and data["totals"].keys() == {"EUR"}
    india = liability_id(w["cid"], "IN.epf")
    assert cleo.get(f"{LIABILITIES}/{india}").status_code == 404
    pension = liability_id(w["cid"], "ZZ.pension")
    # Recording a remittance needs payroll:statutory:submit.
    assert cleo.post(f"{LIABILITIES}/{pension}/remit", json={"remittance_ref": "CH-1"}).status_code == 403
    employee = with_roles(app, w["acme"], "Eve", ["employee"])
    assert employee.get(COMPLIANCE).status_code == 403
    assert employee.get(LIABILITIES).status_code == 403
    assert employee.post(f"{LIABILITIES}/{pension}/remit", json={"remittance_ref": "CH-1"}).status_code == 403
    assert w["acme"].post(f"{LIABILITIES}/{pension}/remit", json={"remittance_ref": "CH-1",
                                                                    "status": "RECONCILED"}).status_code == 422


def test_ec_sec_02_other_tenants_never_reach_compliance_data(app, rc_world):
    w = rc_world
    beta, cobalt = other_tenant(app, "Beta Industries"), other_tenant(app, "Cobalt Labs", currency="USD")
    pension = liability_id(w["cid"], "ZZ.pension")
    for tenant in (beta, cobalt):
        client = tenant["client"]
        by_code, _ = cards(client)
        assert list(by_code) == ["LE01"]
        assert by_code["LE01"]["legal_entity"]["id"] == tenant["entity"]
        assert client.get(COMPLIANCE, params={"legal_entity_id": w["zz01"]}).status_code == 404
        assert client.get(f"{LIABILITIES}/{pension}").status_code == 404
        assert client.post(f"{LIABILITIES}/{pension}/remit", json={"remittance_ref": "X"}).status_code == 404
        assert client.post(f"{LIABILITIES}/{pension}/reconcile", json={}).status_code == 404
        own = ok(client.get(LIABILITIES))
        assert own["total"] == 1 and own["items"][0]["run_id"] == tenant["run"]["id"]
        assert ok(client.get(LIABILITIES, params={"legal_entity_id": w["zz01"]}))["total"] == 0
    assert ok(w["acme"].get(f"{LIABILITIES}/{pension}"))["status"] == "OPEN"
