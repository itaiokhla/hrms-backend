"""Configuration profiles: STARTER_V1 at signup, LEGACY_V1 by migration 0011, no silent drift (E5).

Decisions I-13, I-14; ARCHITECTURE §9; audit 06 §5; EC-CFG-22, EC-MIG-03, EC-MIG-04, EC-MIG-05, EC-CFG-17.
"""

import importlib.util
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.payroll import policy_types as pt
from app.payroll import settings_registry as reg
from app.services.company_service import DEFAULT_LEAVE_TYPES
from tests.helpers import signup_company

ROOT = Path(__file__).resolve().parents[2]
LEAVE_TYPES = [{"code": c, "name": n, "is_paid": paid} for c, n, _q, paid, _u in DEFAULT_LEAVE_TYPES]


def _migration():
    spec = importlib.util.spec_from_file_location("m0011", ROOT / "alembic" / "versions" / "0011_settings_profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(query, **params):
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return result.all() if result.returns_rows else None


def _tenant_rows(company_id):
    rows = _sql("SELECT key, value, locked, source, profile_version, status, effective_from FROM setting_values "
                "WHERE company_id = :c AND scope_type = 'TENANT' AND status = 'ACTIVE' ORDER BY key", c=company_id)
    return {r[0]: r[1:] for r in rows}


def _policies(company_id):
    rows = _sql("SELECT p.type, p.name, v.status, v.fields, v.policy_hash, a.criteria, a.priority FROM policies p "
                "JOIN policy_versions v ON v.policy_id = p.id LEFT JOIN policy_assignments a ON a.policy_id = p.id "
                "WHERE p.company_id = :c ORDER BY p.type", c=company_id)
    return {r[0]: r[1:] for r in rows}


def _session(company_id):
    from app.database.db import SessionLocal, bind_tenant

    db = SessionLocal()
    bind_tenant(db, company_id)
    return db


def _make_legacy(company_id, legacy_settings):
    """Turn a signed-up tenant into what migrations 0001-0010 left: no v2 rows, legacy company_settings."""
    _sql("DELETE FROM setting_values WHERE company_id = :c", c=company_id)
    for table in ("policy_assignments", "policy_versions", "policies"):
        _sql(f"DELETE FROM {table} WHERE company_id = :c", c=company_id)
    _sql("UPDATE companies SET payroll_profile_version = NULL, payroll_profile_applied_at = NULL WHERE id = :c",
         c=company_id)
    for key, value in legacy_settings.items():
        _sql("INSERT INTO company_settings (company_id, key, value, created_at, updated_at) "
             "VALUES (:c, :k, :v, now(), now())", c=company_id, k=key, v=value)


# ---------------------------------------------------------------------------
# STARTER_V1 for new tenants
# ---------------------------------------------------------------------------
def test_ec_cfg_22_new_tenant_gets_exactly_starter_v1_without_statutory_schemes_or_jobs(app):
    acme = signup_company(app, "Starter Co")
    cid = acme.company_id
    expected = reg.profile_rows("STARTER_V1", leave_types=LEAVE_TYPES)
    rows = _tenant_rows(cid)
    assert set(rows) == {r.key for r in expected}
    created = _sql("SELECT created_at FROM companies WHERE id = :c", c=cid)[0][0]
    for row in expected:
        value, locked, source, profile, status, start = rows[row.key]
        assert (value, locked, source, profile) == (row.value, row.locked, "STARTER_PROFILE", "STARTER_V1"), row.key
        assert abs((start - created.date()).days) <= 1
    assert not [k for k in rows if k.startswith("payroll.statutory.")]
    assert rows["payroll.approval.separation_of_duties"][:2] == (True, True)  # ON and locked (I-13)
    assert rows["payroll.overtime.enabled"][0] is False and rows["payroll.enabled"][0] is True
    assert _sql("SELECT payroll_profile_version FROM companies WHERE id = :c", c=cid)[0][0] == "STARTER_V1"
    assert _sql("SELECT count(*) FROM jobs WHERE company_id = :c", c=cid)[0][0] == 0

    policies = _policies(cid)
    starters = {p.type: p for p in pt.starter_policies("STARTER_V1", leave_types=LEAVE_TYPES)}
    assert set(policies) == set(starters)
    for code, starter in starters.items():
        name, status, fields, policy_hash, criteria, priority = policies[code]
        assert (name, status, fields) == (starter.name, starter.status, dict(starter.fields)), code
        assert policy_hash == starter.policy_hash
        if pt.POLICY_TYPES[code].assignable:
            assert (criteria, priority) == ({"everyone": True}, 0), code
    assert policies["OVERTIME_POLICY"][1] == "DRAFT" and policies["ATTENDANCE_PAY_POLICY"][1] == "ACTIVE"
    leave_rows = {r["leave_type_code"]: r["pay_treatment"] for r in policies["LEAVE_PAY_POLICY"][2]["rows"]}
    assert leave_rows == {"casual": "FULL", "sick": "FULL", "earned": "FULL", "maternity": "FULL",
                          "paternity": "FULL", "unpaid": "UNPAID"}
    audit = _sql("SELECT actor_type, details FROM audit_logs WHERE company_id = :c AND module = 'Payroll Settings' "
                 "AND action = 'PROFILE_APPLIED'", c=cid)
    assert len(audit) == 1 and audit[0][0] == "system" and audit[0][1]["profile"] == "STARTER_V1"
    assert audit[0][1]["rows_written"] == len(expected)


def test_ec_cfg_17_each_new_tenant_has_its_own_rows(app):
    acme, beta = signup_company(app, "Own Rows A"), signup_company(app, "Own Rows B")
    a, b = _tenant_rows(acme.company_id), _tenant_rows(beta.company_id)
    assert set(a) == set(b)
    ids = _sql("SELECT company_id, count(*) FROM setting_values GROUP BY company_id")
    assert dict(ids) == {acme.company_id: len(a), beta.company_id: len(b)}


def test_applying_a_profile_again_changes_nothing(app):
    from app.models.company import Company
    from app.payroll.services import profiles

    acme = signup_company(app, "Twice Co")
    before = _tenant_rows(acme.company_id)
    db = _session(acme.company_id)
    try:
        company = db.get(Company, acme.company_id)
        summary = profiles.apply_profile(db, company, "STARTER_V1")
        db.commit()
    finally:
        db.close()
    assert summary["rows_written"] == 0 and summary["policies"] == []
    assert summary["rows_unchanged"] == len(before)
    assert _tenant_rows(acme.company_id) == before


def test_e5_registry_and_platform_default_changes_leave_an_existing_tenant_unchanged(app, monkeypatch):
    from app.payroll.services import settings as engine

    acme = signup_company(app, "Drift Co")
    cid = acme.company_id
    key = "payroll.lop.proration_basis"
    db = _session(cid)
    try:
        today = date.today()
        before = engine.resolve(db, cid, key, on=today)
        assert (before.value, before.level, before.source) == ("CALENDAR_DAYS", "TENANT", "STARTER_PROFILE")
        monkeypatch.setitem(reg.REGISTRY, key, replace(reg.REGISTRY[key], default="WORKING_DAYS"))
        _sql("INSERT INTO platform_setting_values (level, key, value, locked, effective_from, created_by, created_at, "
             "updated_at) VALUES ('PLATFORM', :k, CAST(:v AS jsonb), false, '2000-01-01', 'test', now(), now())",
             k=key, v=json.dumps("FIXED_30"))
        after = engine.resolve(db, cid, key, on=today)
        assert (after.value, after.level, after.row_id) == ("CALENDAR_DAYS", "TENANT", before.row_id)
        # A key without a tenant row (added in a later release) follows the platform value until written.
        _sql("UPDATE setting_values SET status = 'SUPERSEDED' WHERE company_id = :c AND key = :k", c=cid, k=key)
        assert engine.resolve(db, cid, key, on=today).value == "FIXED_30"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# LEGACY_V1 by migration 0011 (existing tenants)
# ---------------------------------------------------------------------------
def test_migration_literals_match_registry_2026_10_3():
    m = _migration()
    assert reg.REGISTRY_VERSION == "2026.10.3"
    assert m.LEGACY_VALUES == {r.key: r.value for r in reg.profile_rows("LEGACY_V1", leave_types=())}
    assert not any(r.locked for r in reg.profile_rows("LEGACY_V1", leave_types=()))
    starters = pt.starter_policies("LEGACY_V1")
    assert [(p["type"], p["name"], p["status"], p["fields"], p["policy_hash"], p["note"]) for p in m.LEGACY_POLICIES] \
        == [(p.type, p.name, p.status, dict(p.fields), p.policy_hash, p.note) for p in starters]
    assert m.LEGACY_VALUES["payroll.migration.legacy_review_acknowledged"] is None
    for legacy in ("1.5", "2", "1.25", "3.0"):
        valid, multiplier = m._legacy_multiplier(json.dumps(float(legacy)))
        expected = pt.starter_policies("LEGACY_V1", legacy_values={"overtime": float(legacy)})
        ot = next(p for p in expected if p.type == "OVERTIME_POLICY").fields
        assert valid and ot["buckets"][0]["multiplier"] == multiplier, legacy
    for bad in ("0", "-1", "11", '"abc"', "true", "1.23456"):
        assert m._legacy_multiplier(bad)[0] is False, bad
    assert m._legacy_value("minutes", "-5") == (False, -5) and m._legacy_value("minutes", "241")[0] is False
    assert m._legacy_value("bool", '"yes"')[0] is False and m._legacy_value("bool", "false") == (True, False)


def test_ec_mig_04_migration_materializes_legacy_v1_idempotently_and_reverts(app):
    m = _migration()
    acme = signup_company(app, "Legacy Co")
    beta = signup_company(app, "Fresh Co")
    cid = acme.company_id
    _make_legacy(cid, {"payrollSeparationOfDuties": "false", "graceTime": "7", "lunchIsPaid": "true",
                       "overtime": "1.5", "pf": "12.0", "tax": "10.0"})
    beta_rows = _tenant_rows(beta.company_id)
    created = _sql("SELECT created_at::date FROM companies WHERE id = :c", c=cid)[0][0]

    from app.database.db import system_engine

    with system_engine.begin() as conn:
        stats = m.apply(conn)
    assert set(stats) == {cid}  # the STARTER_V1 tenant already has a profile
    assert stats[cid]["rows_written"] == len(m.LEGACY_VALUES)
    assert stats[cid]["legacy_values"] == {"payrollSeparationOfDuties": False, "graceTime": 7, "lunchIsPaid": True,
                                           "overtime": "1.5"}
    rows = _tenant_rows(cid)
    expected = reg.profile_values("LEGACY_V1", leave_types=(), legacy_values={
        "payrollSeparationOfDuties": False, "graceTime": 7, "lunchIsPaid": True})
    assert set(rows) == set(expected)
    for key, value in expected.items():
        assert rows[key][0] == reg.to_json(key, value), key
        assert rows[key][1:5] == (False, "LEGACY_MIGRATION", "LEGACY_V1", "ACTIVE") and rows[key][5] == created
    assert rows["payroll.attendance.enabled"][0] is True and rows["payroll.lop.proration_basis"][0] == "WORKING_DAYS"
    assert rows["payroll.leave.enabled"][0] is False and rows["payroll.overtime.enabled"][0] is False
    policies = _policies(cid)
    assert {k: v[1] for k, v in policies.items()} == {
        "ATTENDANCE_PAY_POLICY": "ACTIVE", "PAYSLIP_TEMPLATE": "ACTIVE", "OVERTIME_POLICY": "DRAFT",
        "LATE_PENALTY_POLICY": "DRAFT"}
    assert policies["ATTENDANCE_PAY_POLICY"][2]["unmarked_day"] == "PRESENT"
    assert policies["OVERTIME_POLICY"][2]["buckets"][0]["multiplier"] == "1.5"  # EC-MIG-04: copied, switch OFF
    assert policies["LATE_PENALTY_POLICY"][2]["penalty_type"] == "FIXED_AMOUNT"  # EC-MIG-03: draft, not applied
    assert _sql("SELECT payroll_profile_version FROM companies WHERE id = :c", c=cid)[0][0] == "LEGACY_V1"
    assert _tenant_rows(beta.company_id) == beta_rows

    # The legacy facade now reads the migrated v2 values; pf and tax stay read-only legacy rows (EC-MIG-05).
    from app.services import settings_service

    db = _session(cid)
    try:
        assert settings_service.get_values(db, cid, ["graceTime", "lunchIsPaid", "payrollSeparationOfDuties", "pf"]) \
            == {"graceTime": 7, "lunchIsPaid": True, "payrollSeparationOfDuties": False, "pf": 12.0}
    finally:
        db.close()
    assert "pf" not in reg.REGISTRY and "tax" not in reg.ALIASES

    with system_engine.begin() as conn:
        assert m.apply(conn) == {}
    assert _tenant_rows(cid) == rows

    with system_engine.begin() as conn:
        reverted = m.revert(conn)
    assert reverted[cid] == {"rows_deleted": len(m.LEGACY_VALUES), "policies_deleted": 4}
    assert reverted[beta.company_id] == {"rows_deleted": 0, "policies_deleted": 0}
    assert _tenant_rows(cid) == {} and _policies(cid) == {}
    assert _sql("SELECT payroll_profile_version FROM companies WHERE id = :c", c=cid)[0][0] is None
    assert _tenant_rows(beta.company_id) == beta_rows

    # Upgrade again after the rollback, then roll back again: only the new run is undone.
    with system_engine.begin() as conn:
        again = m.apply(conn)
    assert again[cid]["rows_written"] == len(m.LEGACY_VALUES)
    with system_engine.begin() as conn:
        assert m.revert(conn)[cid]["policies_deleted"] == 4
    audit = _sql("SELECT action FROM audit_logs WHERE company_id = :c AND details->>'migration' = :m ORDER BY id",
                 c=cid, m=m.MARKER)
    assert [a for (a,) in audit] == ["PROFILE_APPLIED", "PROFILE_REVERTED", "PROFILE_APPLIED", "PROFILE_REVERTED"]


def test_migration_skips_invalid_legacy_values_and_keeps_configured_keys(app):
    m = _migration()
    acme = signup_company(app, "Messy Co")
    cid = acme.company_id
    _make_legacy(cid, {"graceTime": "-5", "lunchIsPaid": '"yes"', "overtime": "25"})
    _sql("INSERT INTO setting_values (company_id, key, scope_type, value, locked, effective_from, status, row_version, "
         "source, created_at, updated_at) VALUES (:c, 'payroll.enabled', 'TENANT', 'false', false, '2026-01-01', "
         "'ACTIVE', 1, 'ADMIN', now(), now())", c=cid)
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        stats = m.apply(conn)[cid]
    assert stats["skipped_legacy_values"] == {"graceTime": -5, "lunchIsPaid": "yes", "overtime": 25}
    assert stats["legacy_values"] == {} and stats["rows_written"] == len(m.LEGACY_VALUES) - 1
    rows = _tenant_rows(cid)
    assert rows["attendance.grace_minutes"][0] == 15 and rows["attendance.lunch_is_paid"][0] is False
    assert rows["payroll.enabled"][0] is False and rows["payroll.enabled"][2] == "ADMIN"
    assert _policies(cid)["OVERTIME_POLICY"][2]["buckets"][0]["multiplier"] is None


def test_unknown_profile_is_rejected(app):
    from app.core.errors import AppError
    from app.models.company import Company
    from app.payroll.services import profiles

    acme = signup_company(app, "Unknown Profile Co")
    db = _session(acme.company_id)
    try:
        with pytest.raises(AppError) as err:
            profiles.apply_profile(db, db.get(Company, acme.company_id), "NOPE_V9")
        assert err.value.code == "UNKNOWN_PROFILE"
    finally:
        db.close()
