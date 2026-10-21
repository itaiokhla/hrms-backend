"""Migration 0009 (payroll platform): row-level security, composite keys, grants, overlap triggers and checks.

Everything runs as the RLS-restricted application role (``engine``) unless the test is about
platform-owned rows, which the owner (``system_engine``) writes the way operators and the worker do.
"""

import importlib.util
import json
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.helpers import create_employee, invite_and_activate

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0009_payroll_platform.py"
HASH = "a" * 64
OTHER_HASH = "b" * 64
# Probe keys and names, so rows a tenant's starter profile writes at signup never collide with these.
KEY, OTHER_KEY = "probe.schema.enabled", "probe.schema.other"
POLICY_TYPE = "PROBE_POLICY"
# These tables' rate-reference guard rejects a foreign company before row-level security does.
RATE_GUARDED = ("fx_snapshot_rates", "fx_conversions")

CHECK, UNIQUE, FOREIGN_KEY, EXCLUSION, DENIED = "23514", "23505", "23503", "23P01", "42501"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_0009", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module", autouse=True)
def app_role_platform_grants():
    """Grant the app role what the 0009 upgrade grants an existing deployment's app role.

    The test database gets its app-role grants from ``app.cli.grant_app_role``, which does not cover
    the platform tables of 0009 yet (see test_fresh_database_grant_app_role_covers_platform_tables).
    """
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        _migration().grant_app_roles(conn)


@contextmanager
def tenant(company_id):
    """A transaction on the application role bound to one company (None = no tenant context)."""
    from app.database.db import engine

    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.company_id', :c, true)"),
                     {"c": "" if company_id is None else str(company_id)})
        yield conn


@contextmanager
def owner():
    from app.database.db import system_engine

    with system_engine.begin() as conn:
        yield conn


def expect_error(conn, code, sql, **params):
    """Run one statement in a savepoint and assert it fails with SQLSTATE `code`."""
    with pytest.raises(DBAPIError) as info:
        with conn.begin_nested():
            conn.execute(text(sql), params)
    assert info.value.orig.pgcode == code, info.value.orig
    return info.value


def scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar()


def admin_user_id(company_id):
    with owner() as conn:
        return scalar(conn, "SELECT id FROM users WHERE company_id = :c ORDER BY id LIMIT 1", c=company_id)


def insert(conn, table, **values):
    columns = ", ".join(values)
    marks = ", ".join(f":{k}" for k in values)
    return scalar(conn, f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id", **values)


def fx_rate(conn, company_id, rate="83.250000000000", status="ACTIVE", base="USD", quote="INR", as_of="2026-10-01"):
    return insert(conn, "fx_rates", company_id=company_id, base_currency=base, quote_currency=quote, rate=rate,
                  source="MANUAL" if company_id else "ECB", as_of=as_of, fetched_at="2026-10-01T00:00:00Z",
                  status=status)


def seed_tenant(conn, company_id, user_id):
    """One row in every 0009 tenant table, written through the tenant's own RLS context."""
    ids = {}
    ids["setting_values"] = insert(conn, "setting_values", company_id=company_id, key=KEY,
                                   scope_type="TENANT", value="true", effective_from="2026-01-01", status="ACTIVE")
    ids["setting_change_requests"] = insert(conn, "setting_change_requests", company_id=company_id,
                                            setting_value_id=ids["setting_values"], requested_by_user_id=user_id)
    ids["policies"] = insert(conn, "policies", company_id=company_id, type=POLICY_TYPE, name="Standard")
    ids["policy_versions"] = insert(conn, "policy_versions", company_id=company_id, policy_id=ids["policies"],
                                    version=1, fields='{"multiplier": "1.5"}', effective_from="2026-01-01")
    ids["policy_assignments"] = insert(conn, "policy_assignments", company_id=company_id, policy_id=ids["policies"],
                                       effective_from="2026-01-01")
    ids["payroll_tables"] = insert(conn, "payroll_tables", company_id=company_id, code="PROBE_SLAB", name="PT",
                                   kind="SLAB", currency="INR", effective_from="2026-01-01")
    ids["payroll_table_rows"] = insert(conn, "payroll_table_rows", company_id=company_id,
                                       table_id=ids["payroll_tables"], lower="0", upper="10000", value="200")
    rate_id = fx_rate(conn, company_id)
    ids["fx_snapshots"] = insert(conn, "fx_snapshots", company_id=company_id, purpose="RUN",
                                 captured_at="2026-10-01T00:00:00Z")
    ids["fx_snapshot_rates"] = insert(conn, "fx_snapshot_rates", company_id=company_id,
                                      snapshot_id=ids["fx_snapshots"], fx_rate_id=rate_id, base_currency="USD",
                                      quote_currency="INR", rate="83.250000000000", rate_type="MID",
                                      source="MANUAL", as_of="2026-10-01")
    ids["fx_conversions"] = insert(conn, "fx_conversions", company_id=company_id, source_amount="100.0000",
                                   source_currency="USD", target_currency="INR", purpose="CONTRACT_TO_PAY",
                                   fx_rate_id=rate_id, rate="83.250000000000", quotation="1 USD = rate INR",
                                   rate_date="2026-10-01", rounding_mode="HALF_UP", result_amount="8325.0000")
    ids["idempotency_keys"] = insert(conn, "idempotency_keys", company_id=company_id, user_id=user_id,
                                     key="key-0001", method="POST", path="/api/v1/payroll/runs",
                                     request_hash=HASH, expires_at="2099-01-01T00:00:00Z")
    ids["outbox_events"] = insert(conn, "outbox_events", company_id=company_id, topic="payroll.run.finalized")
    ids["approval_requests"] = insert(conn, "approval_requests", company_id=company_id, module="overtime",
                                      subject_type="overtime_entry", subject_id=1, requested_by_user_id=user_id)
    ids["approval_request_steps"] = insert(conn, "approval_request_steps", company_id=company_id,
                                           request_id=ids["approval_requests"], step_order=1,
                                           approver_type="primary_manager", status="pending")
    return ids


# ---------------------------------------------------------------------------
# Tenant tables: row-level security, composite keys, model registration
# ---------------------------------------------------------------------------
def test_ec_sec_01_tenant_tables_match_the_models_and_enforce_rls():
    from app.models import TENANT_TABLES

    tables = set(_migration().TENANT_TABLES)
    assert tables <= set(TENANT_TABLES)
    with owner() as conn:
        rows = conn.execute(text(
            "SELECT c.relname, c.relrowsecurity, "
            "EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = 'tenant_isolation') "
            "FROM pg_class c WHERE c.relname = ANY(:t) AND c.relkind = 'r'"), {"t": list(tables)}).fetchall()
    assert {name for name, rls, policy in rows if rls and policy} == tables


def test_ec_sec_01_every_new_tenant_table_is_isolated_across_three_tenants(three_companies):
    companies = [three_companies[k].company_id for k in ("A", "B", "C")]
    seeded = {}
    for cid in companies:
        with tenant(cid) as conn:
            seeded[cid] = seed_tenant(conn, cid, admin_user_id(cid))
    tables = list(seeded[companies[0]])
    assert set(tables) == set(_migration().TENANT_TABLES)

    with tenant(None) as conn:  # no tenant context: nothing at all
        assert {t: scalar(conn, f"SELECT count(*) FROM {t}") for t in tables} == {t: 0 for t in tables}

    for cid in companies:
        others = [c for c in companies if c != cid]
        with tenant(cid) as conn:
            for table in tables:
                visible = conn.execute(text(f"SELECT id, company_id FROM {table}")).fetchall()
                assert {owner_id for _, owner_id in visible} == {cid}, table
                assert seeded[cid][table] in {row_id for row_id, _ in visible}, table
                for other in others:
                    foreign_id = seeded[other][table]
                    assert scalar(conn, f"SELECT count(*) FROM {table} WHERE id = :i", i=foreign_id) == 0, table
                    assert conn.execute(text(f"UPDATE {table} SET updated_at = now() WHERE id = :i"),
                                        {"i": foreign_id}).rowcount == 0, table
                    assert conn.execute(text(f"DELETE FROM {table} WHERE id = :i"),
                                        {"i": foreign_id}).rowcount == 0, table
                # Moving an own row to another tenant, or writing a copy for it, is refused.
                expected = FOREIGN_KEY if table in RATE_GUARDED else DENIED
                expect_error(conn, expected, f"UPDATE {table} SET company_id = :o WHERE id = :i",
                             o=others[0], i=seeded[cid][table])
                expect_error(
                    conn, expected,
                    f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, to_jsonb(x) "
                    f"|| jsonb_build_object('id', nextval('{table}_id_seq'), 'company_id', :o))).* "
                    f"FROM {table} x WHERE x.id = :i",
                    o=others[0], i=seeded[cid][table])
            # Without a rate reference the policy itself refuses the foreign row.
            expect_error(conn, DENIED, "UPDATE fx_conversions SET fx_rate_id = NULL, company_id = :o WHERE id = :i",
                         o=others[0], i=seeded[cid]["fx_conversions"])

    with owner() as conn:  # nothing was changed or deleted across tenants
        for cid in companies:
            for table in tables:
                assert scalar(conn, f"SELECT company_id FROM {table} WHERE id = :i", i=seeded[cid][table]) == cid


def test_ec_sec_01_composite_foreign_keys_never_reference_another_tenant(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    user_b = admin_user_id(b)
    with tenant(b) as conn:
        rows_b = seed_tenant(conn, b, user_b)
    with owner() as conn:  # the owner bypasses RLS: only the composite keys stop these
        own_request = insert(conn, "approval_requests", company_id=a, module="overtime", subject_type="x",
                             subject_id=1)
        value_b = _setting(conn, b, "2027-01-01", status="PENDING_APPROVAL")
        cases = [
            ("setting_change_requests", dict(setting_value_id=value_b, requested_by_user_id=user_b)),
            ("policy_versions", dict(policy_id=rows_b["policies"], version=9, fields="{}",
                                     effective_from="2026-01-01")),
            ("policy_assignments", dict(policy_id=rows_b["policies"], effective_from="2026-01-01")),
            ("payroll_table_rows", dict(table_id=rows_b["payroll_tables"], value="1")),
            ("fx_snapshot_rates", dict(snapshot_id=rows_b["fx_snapshots"], fx_rate_id=fx_rate(conn, None),
                                       base_currency="USD", quote_currency="EUR", rate="1", rate_type="MID",
                                       source="x", as_of="2026-10-01")),
            ("idempotency_keys", dict(user_id=user_b, key="k", method="POST", path="/x", request_hash=HASH,
                                      expires_at="2099-01-01T00:00:00Z")),
            ("approval_request_steps", dict(request_id=rows_b["approval_requests"], step_order=5,
                                            approver_type="primary_manager")),
            ("approval_request_steps", dict(request_id=own_request, step_order=1, approver_type="specific_user",
                                            specific_user_id=user_b)),
        ]
        for table, values in cases:
            cols = ["company_id", *values]
            expect_error(conn, FOREIGN_KEY,
                         f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                         company_id=a, **values)


def test_ec_cfg_17_orm_reads_of_settings_and_policies_stay_in_the_tenant(three_companies):
    from app.database.db import SessionLocal, TenantContextError, bind_tenant
    from app.models.payroll_config import Policy, SettingValue

    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    for cid in (a, b):
        with tenant(cid) as conn:
            seed_tenant(conn, cid, admin_user_id(cid))
    db = SessionLocal()
    try:
        bind_tenant(db, a)
        assert {s.company_id for s in db.query(SettingValue).all()} == {a}
        assert {p.company_id for p in db.query(Policy).all()} == {a}
        db.add(Policy(company_id=b, type=POLICY_TYPE, name="Injected"))
        with pytest.raises(TenantContextError):
            db.flush()
    finally:
        db.rollback()
        db.close()


# ---------------------------------------------------------------------------
# Settings engine v2
# ---------------------------------------------------------------------------
def _setting(conn, cid, effective_from, effective_to=None, status="ACTIVE", key=KEY,
             scope_type="TENANT", scope_id=None, **extra):
    return insert(conn, "setting_values", company_id=cid, key=key, scope_type=scope_type, scope_id=scope_id,
                  value=json.dumps(extra.pop("value", True)), effective_from=effective_from,
                  effective_to=effective_to, status=status, **extra)


SETTING_SQL = ("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, effective_from, "
               "effective_to, status) VALUES (:c, :k, :st, :sid, 'true', :f, :t, :s)")


def test_ec_cfg_01_active_setting_windows_never_overlap(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as conn:
        first = _setting(conn, a, "2026-01-01")
        base = dict(c=a, k=KEY, st="TENANT", sid=None, s="ACTIVE")
        expect_error(conn, EXCLUSION, SETTING_SQL, **base, f="2026-05-01", t=None)
        expect_error(conn, EXCLUSION, SETTING_SQL, **base, f="2025-01-01", t="2026-01-01")  # inclusive end
        # Drafts awaiting approval, other keys and other scopes may overlap.
        pending = _setting(conn, a, "2026-05-01", status="PENDING_APPROVAL")
        _setting(conn, a, "2026-05-01", key=OTHER_KEY)
        _setting(conn, a, "2026-05-01", scope_type="BRANCH", scope_id=7)
        _setting(conn, a, "2026-05-01", scope_type="LEGAL_ENTITY", scope_id=7)
        expect_error(conn, EXCLUSION, SETTING_SQL, **{**base, "st": "BRANCH", "sid": 7}, f="2026-06-01", t=None)
        _setting(conn, a, "2026-06-01", scope_type="BRANCH", scope_id=8)
        # Activating the pending row while the first is open overlaps; close the first, then it works.
        expect_error(conn, EXCLUSION, "UPDATE setting_values SET status = 'ACTIVE' WHERE id = :i", i=pending)
        conn.execute(text("UPDATE setting_values SET effective_to = '2026-04-30' WHERE id = :i"), {"i": first})
        conn.execute(text("UPDATE setting_values SET status = 'ACTIVE' WHERE id = :i"), {"i": pending})
        # Widening a closed window back over its successor is rejected.
        expect_error(conn, EXCLUSION, "UPDATE setting_values SET effective_to = NULL WHERE id = :i", i=first)
        # Superseding releases the window.
        conn.execute(text("UPDATE setting_values SET status = 'SUPERSEDED' WHERE id = :i"), {"i": pending})
        _setting(conn, a, "2026-05-01")
    with tenant(b) as conn:  # another tenant's identical window is independent
        _setting(conn, b, "2026-01-01")


def test_setting_values_scope_status_source_and_window_checks(three_companies):
    a = three_companies["A"].company_id
    user = admin_user_id(a)
    with tenant(a) as conn:
        row = conn.execute(text(
            "SELECT locked, row_version, source, profile_version FROM setting_values WHERE id = :i"),
            {"i": _setting(conn, a, "2026-01-01")}).one()
        assert tuple(row) == (False, 1, "ADMIN", None)
        base = dict(c=a, k=KEY, sid=None, f="2027-01-01", t=None, s="ACTIVE")
        expect_error(conn, CHECK, SETTING_SQL, **{**base, "st": "TENANT", "sid": 1})
        expect_error(conn, CHECK, SETTING_SQL, **{**base, "st": "BRANCH"})
        expect_error(conn, CHECK, SETTING_SQL, **{**base, "st": "PARTNER"})  # partner level is platform-owned
        expect_error(conn, CHECK, SETTING_SQL, **{**base, "st": "TENANT", "s": "DRAFT"})
        expect_error(conn, CHECK, SETTING_SQL, **{**base, "st": "TENANT", "f": "2027-02-01", "t": "2027-01-31"})
        sql = ("INSERT INTO setting_values (company_id, key, scope_type, value, effective_from, status, source, "
               "profile_version) VALUES (:c, :k, 'TENANT', 'true', :f, 'ACTIVE', :src, :pv)")
        # Profile rows carry their profile version (decisions I-13, I-14); unknown sources are refused.
        expect_error(conn, CHECK, sql, c=a, k="probe.x", f="2026-01-01", src="STARTER_PROFILE", pv=None)
        expect_error(conn, CHECK, sql, c=a, k="probe.x", f="2026-01-01", src="LEGACY_MIGRATION", pv=None)
        expect_error(conn, CHECK, sql, c=a, k="probe.x", f="2026-01-01", src="IMPORT", pv=None)
        for index, (src, pv) in enumerate([("STARTER_PROFILE", "STARTER_V1"), ("LEGACY_MIGRATION", "LEGACY_V1"),
                                           ("PACK_LOCK", None), ("PARTNER_PROVISIONING", None)]):
            conn.execute(text(sql), dict(c=a, k=f"probe.k{index}", f="2026-01-01", src=src, pv=pv))
        expect_error(conn, CHECK, "UPDATE setting_values SET row_version = 0 WHERE key = 'probe.k0'")


def test_ec_cfg_12_maker_never_approves_own_setting_change(three_companies, app):
    a_admin = three_companies["A"]
    a = a_admin.company_id
    maker = admin_user_id(a)
    checker = invite_and_activate(app, a_admin, create_employee(a_admin, "Chris", "Checker"), ["hr_admin"]).user_id
    with tenant(a) as conn:
        value = _setting(conn, a, "2026-01-01", status="PENDING_APPROVAL", created_by_user_id=maker)
        expect_error(conn, CHECK, "UPDATE setting_values SET approved_by_user_id = :u WHERE id = :i", u=maker, i=value)
        request = insert(conn, "setting_change_requests", company_id=a, setting_value_id=value,
                         requested_by_user_id=maker)
        # One open request per value; a decision needs its time; the maker cannot approve.
        expect_error(conn, UNIQUE, "INSERT INTO setting_change_requests (company_id, setting_value_id, "
                     "requested_by_user_id) VALUES (:c, :v, :u)", c=a, v=value, u=maker)
        expect_error(conn, CHECK, "UPDATE setting_change_requests SET decision = 'APPROVED', decided_by_user_id = :u "
                     "WHERE id = :i", u=checker, i=request)
        expect_error(conn, CHECK, "UPDATE setting_change_requests SET decision = 'APPROVED', decided_by_user_id = :u, "
                     "decided_at = now() WHERE id = :i", u=maker, i=request)
        expect_error(conn, CHECK, "UPDATE setting_change_requests SET decision = 'MAYBE', decided_at = now() "
                     "WHERE id = :i", i=request)
        conn.execute(text("UPDATE setting_change_requests SET decision = 'APPROVED', decided_by_user_id = :u, "
                          "decided_at = now() WHERE id = :i"), {"u": checker, "i": request})
        conn.execute(text("UPDATE setting_values SET status = 'ACTIVE', approved_by_user_id = :u, approved_at = now() "
                          "WHERE id = :i"), {"u": checker, "i": value})


def test_platform_setting_values_overlap_and_level_rules():
    with owner() as conn:
        partner = insert(conn, "partners", name="PayEase", slug="payease")
        other = insert(conn, "partners", name="Bureau", slug="bureau")
        sql = ("INSERT INTO platform_setting_values (level, partner_id, key, value, effective_from, effective_to, "
               "created_by) VALUES (:l, :p, :k, 'true', :f, :t, 'ops@platform')")
        conn.execute(text(sql), dict(l="PLATFORM", p=None, k="payroll.enabled", f="2026-01-01", t="2026-12-31"))
        expect_error(conn, EXCLUSION, sql, l="PLATFORM", p=None, k="payroll.enabled", f="2026-12-31", t=None)
        conn.execute(text(sql), dict(l="PLATFORM", p=None, k="payroll.enabled", f="2027-01-01", t=None))
        conn.execute(text(sql), dict(l="PLATFORM", p=None, k="payroll.overtime.enabled", f="2026-01-01", t=None))
        # Each partner has its own timeline, independent of the platform's and of other partners'.
        conn.execute(text(sql), dict(l="PARTNER", p=partner, k="payroll.enabled", f="2026-01-01", t=None))
        conn.execute(text(sql), dict(l="PARTNER", p=other, k="payroll.enabled", f="2026-01-01", t=None))
        expect_error(conn, EXCLUSION, sql, l="PARTNER", p=partner, k="payroll.enabled", f="2026-06-01", t=None)
        expect_error(conn, CHECK, sql, l="PLATFORM", p=partner, k="probe.x", f="2026-01-01", t=None)
        expect_error(conn, CHECK, sql, l="PARTNER", p=None, k="probe.x", f="2026-01-01", t=None)
        expect_error(conn, CHECK, sql, l="TENANT", p=None, k="probe.x", f="2026-01-01", t=None)
        expect_error(conn, CHECK, sql, l="PLATFORM", p=None, k="probe.x", f="2026-02-01", t="2026-01-01")


def test_app_role_reads_platform_settings_and_capabilities_but_never_writes_them(three_companies):
    a = three_companies["A"].company_id
    with owner() as conn:
        conn.execute(text("INSERT INTO platform_setting_values (level, key, value, effective_from, created_by) "
                          "VALUES ('PLATFORM', 'payroll.enabled', 'false', '2026-01-01', 'ops')"))
        conn.execute(text("INSERT INTO country_capabilities (alpha2, capability_level, updated_by) "
                          "VALUES ('IN', 'CONFIG_ONLY', 'ops')"))
    with tenant(a) as conn:
        assert scalar(conn, "SELECT count(*) FROM platform_setting_values") == 1
        assert scalar(conn, "SELECT capability_level FROM country_capabilities WHERE alpha2 = 'IN'") == "CONFIG_ONLY"
        expect_error(conn, DENIED, "INSERT INTO platform_setting_values (level, key, value, effective_from, "
                     "created_by) VALUES ('PLATFORM', 'payroll.x', 'true', '2026-01-01', 'tenant')")
        expect_error(conn, DENIED, "UPDATE platform_setting_values SET value = 'true'")
        expect_error(conn, DENIED, "DELETE FROM platform_setting_values")
        expect_error(conn, DENIED, "INSERT INTO country_capabilities (alpha2, capability_level, updated_by) "
                     "VALUES ('AE', 'VERIFIED', 'tenant')")
        expect_error(conn, DENIED, "UPDATE country_capabilities SET capability_level = 'VERIFIED'")


def test_country_capabilities_are_not_seeded_and_validate_levels():
    with owner() as conn:
        # Absence of a row means the code default; nothing may depend on migration-seeded rows.
        assert scalar(conn, "SELECT count(*) FROM country_capabilities") == 0
        sql = "INSERT INTO country_capabilities (alpha2, capability_level, updated_by) VALUES (:a, :l, 'ops')"
        for index, level in enumerate(_migration().CAPABILITY_LEVELS):
            conn.execute(text(sql), {"a": f"Q{chr(65 + index)}", "l": level})
        expect_error(conn, CHECK, sql, a="ZZ", l="FINAL")
        expect_error(conn, CHECK, sql, a="in", l="CONFIG_ONLY")
        expect_error(conn, UNIQUE, sql, a="QA", l="CONFIG_ONLY")


# ---------------------------------------------------------------------------
# Policies and payroll tables
# ---------------------------------------------------------------------------
VERSION_SQL = ("INSERT INTO policy_versions (company_id, policy_id, version, fields, effective_from, effective_to, "
               "status, policy_hash, activated_at) VALUES (:c, :p, :v, '{}', :f, :t, :s, :h, :at)")


def _version(conn, cid, policy, version, effective_from, effective_to=None, status="ACTIVE"):
    active = status != "DRAFT"
    return insert(conn, "policy_versions", company_id=cid, policy_id=policy, version=version,
                  fields='{"multiplier": "2"}', effective_from=effective_from, effective_to=effective_to,
                  status=status, policy_hash=HASH if active else None,
                  activated_at="2026-01-01T00:00:00Z" if active else None)


def test_ec_cfg_18_active_policy_versions_never_overlap(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as conn:
        policy = insert(conn, "policies", company_id=a, type=POLICY_TYPE, name="Standard")
        sibling = insert(conn, "policies", company_id=a, type=POLICY_TYPE, name="Shop floor")
        v1 = _version(conn, a, policy, 1, "2026-01-01", "2026-06-30")
        expect_error(conn, EXCLUSION, VERSION_SQL, c=a, p=policy, v=2, f="2026-06-30", t=None, s="ACTIVE", h=HASH,
                     at="2026-06-01T00:00:00Z")
        expect_error(conn, CHECK, VERSION_SQL, c=a, p=policy, v=2, f="2026-08-01", t="2026-07-31", s="ACTIVE", h=HASH,
                     at="2026-06-01T00:00:00Z")
        _version(conn, a, policy, 2, "2026-07-01")
        draft = _version(conn, a, policy, 3, "2026-08-01", status="DRAFT")  # drafts may overlap
        _version(conn, a, sibling, 1, "2026-01-01")  # another policy has its own timeline
        # Activating the overlapping draft fails until the current version is closed.
        expect_error(conn, EXCLUSION, "UPDATE policy_versions SET status = 'ACTIVE', policy_hash = :h, "
                     "activated_at = now() WHERE id = :i", h=OTHER_HASH, i=draft)
        expect_error(conn, UNIQUE, VERSION_SQL, c=a, p=policy, v=1, f="2030-01-01", t=None, s="DRAFT", h=None, at=None)
        assert scalar(conn, "SELECT status FROM policy_versions WHERE id = :i", i=v1) == "ACTIVE"


def test_ec_cfg_14_activated_policy_version_is_immutable(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as conn:
        policy = insert(conn, "policies", company_id=a, type=POLICY_TYPE, name="Standard")
        draft = _version(conn, a, policy, 1, "2026-01-01", status="DRAFT")
        conn.execute(text("UPDATE policy_versions SET fields = '{\"multiplier\": \"3\"}' WHERE id = :i"), {"i": draft})
        expect_error(conn, CHECK, "UPDATE policy_versions SET status = 'ACTIVE' WHERE id = :i", i=draft)  # no hash
        expect_error(conn, CHECK, "UPDATE policy_versions SET policy_hash = 'XYZ' WHERE id = :i", i=draft)
        conn.execute(text("UPDATE policy_versions SET status = 'ACTIVE', policy_hash = :h, activated_at = now() "
                          "WHERE id = :i"), {"h": HASH, "i": draft})
        for change in ("fields = '{}'::jsonb", f"policy_hash = '{OTHER_HASH}'", "effective_from = '2026-02-01'",
                       "version = 2", "status = 'DRAFT'", "activated_at = now() + interval '1 day'"):
            expect_error(conn, DENIED, f"UPDATE policy_versions SET {change} WHERE id = :i", i=draft)
        # Only closing the window and superseding are allowed; a superseded version is final.
        conn.execute(text("UPDATE policy_versions SET effective_to = '2026-12-31' WHERE id = :i"), {"i": draft})
        conn.execute(text("UPDATE policy_versions SET status = 'SUPERSEDED' WHERE id = :i"), {"i": draft})
        expect_error(conn, DENIED, "UPDATE policy_versions SET status = 'ACTIVE' WHERE id = :i", i=draft)
        expect_error(conn, DENIED, "UPDATE policy_versions SET effective_to = NULL WHERE id = :i", i=draft)


def test_ec_cfg_16_policy_with_versions_cannot_be_deleted_and_names_are_per_tenant(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as conn:
        policy = insert(conn, "policies", company_id=a, type=POLICY_TYPE, name="Standard")
        _version(conn, a, policy, 1, "2026-01-01")
        expect_error(conn, FOREIGN_KEY, "DELETE FROM policies WHERE id = :i", i=policy)
        expect_error(conn, UNIQUE, "INSERT INTO policies (company_id, type, name) VALUES (:c, :t, 'Standard')",
                     c=a, t=POLICY_TYPE)
        expect_error(conn, CHECK, "UPDATE policies SET status = 'DELETED' WHERE id = :i", i=policy)
        insert(conn, "policies", company_id=a, type=POLICY_TYPE + "_LATE", name="Standard")
        conn.execute(text("UPDATE policies SET status = 'ARCHIVED' WHERE id = :i"), {"i": policy})
        insert(conn, "policy_assignments", company_id=a, policy_id=policy, effective_from="2026-01-01",
               criteria='{"branch_ids": [1]}', priority=10)
        expect_error(conn, CHECK, "INSERT INTO policy_assignments (company_id, policy_id, criteria, effective_from) "
                     "VALUES (:c, :p, '[]', '2026-01-01')", c=a, p=policy)
        expect_error(conn, CHECK, "INSERT INTO policy_assignments (company_id, policy_id, effective_from, status) "
                     "VALUES (:c, :p, '2026-01-01', 'PAUSED')", c=a, p=policy)
    with tenant(b) as conn:
        insert(conn, "policies", company_id=b, type=POLICY_TYPE, name="Standard")


def test_payroll_tables_active_versions_and_rows(three_companies):
    a = three_companies["A"].company_id
    sql = ("INSERT INTO payroll_tables (company_id, code, name, kind, effective_from, effective_to, status) "
           "VALUES (:c, :cd, 'T', :k, :f, :t, :s) RETURNING id")
    with tenant(a) as conn:
        first = scalar(conn, sql, c=a, cd="PROBE", k="SLAB", f="2026-01-01", t="2026-03-31", s="ACTIVE")
        expect_error(conn, EXCLUSION, sql, c=a, cd="PROBE", k="SLAB", f="2026-03-01", t=None, s="ACTIVE")
        expect_error(conn, UNIQUE, sql, c=a, cd="PROBE", k="SLAB", f="2026-01-01", t=None, s="DRAFT")
        scalar(conn, sql, c=a, cd="PROBE", k="SLAB", f="2026-03-15", t=None, s="DRAFT")
        scalar(conn, sql, c=a, cd="PROBE", k="SLAB", f="2026-04-01", t=None, s="ACTIVE")
        expect_error(conn, CHECK, sql, c=a, cd="X", k="CURVE", f="2026-01-01", t=None, s="DRAFT")
        row = ("INSERT INTO payroll_table_rows (company_id, table_id, lower, upper, key, value, rate) "
               "VALUES (:c, :t, :lo, :up, :k, :v, :r)")
        conn.execute(text(row), dict(c=a, t=first, lo="0", up="15000.0000", k=None, v="0", r=None))
        conn.execute(text(row), dict(c=a, t=first, lo="15000.0001", up=None, k=None, v=None, r="0.020000"))
        expect_error(conn, CHECK, row, c=a, t=first, lo="10", up="5", k=None, v="1", r=None)
        expect_error(conn, CHECK, row, c=a, t=first, lo="0", up="1", k=None, v=None, r=None)
        conn.execute(text(row), dict(c=a, t=first, lo=None, up=None, k="MH", v="200", r=None))
        expect_error(conn, UNIQUE, row, c=a, t=first, lo=None, up=None, k="MH", v="300", r=None)


# ---------------------------------------------------------------------------
# FX rates, snapshots and conversions
# ---------------------------------------------------------------------------
def test_fx_rates_platform_rows_are_shared_and_tenant_rows_private(three_companies):
    a, b, c = (three_companies[k].company_id for k in ("A", "B", "C"))
    with owner() as conn:
        platform = fx_rate(conn, None, rate="3.672500000000", base="USD", quote="AED")
    with tenant(a) as conn:
        own_a = fx_rate(conn, a)
    with tenant(b) as conn:
        own_b = fx_rate(conn, b)

    visible = {}
    for cid in (a, b, c, None):
        with tenant(cid) as conn:
            visible[cid] = {r[0] for r in conn.execute(text("SELECT id FROM fx_rates"))}
    assert visible == {a: {platform, own_a}, b: {platform, own_b}, c: {platform}, None: {platform}}

    from app.database.db import SessionLocal, bind_tenant
    from app.models.catalogue import FxRate

    db = SessionLocal()
    try:
        bind_tenant(db, c)
        assert [r.id for r in db.query(FxRate).all()] == [platform]
    finally:
        db.close()


def test_ec_mon_07_tenant_cannot_write_platform_or_foreign_fx_rates(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with owner() as conn:
        platform = fx_rate(conn, None)
    with tenant(b) as conn:
        foreign = fx_rate(conn, b)
    sql = ("INSERT INTO fx_rates (company_id, base_currency, quote_currency, rate, source, as_of, fetched_at, status) "
           "VALUES (:c, :base, :quote, :rate, 'MANUAL', '2026-10-01', now(), 'PENDING_APPROVAL')")
    good = dict(base="USD", quote="INR", rate="83.1")
    with tenant(a) as conn:
        expect_error(conn, DENIED, sql, c=None, **good)  # a platform row
        expect_error(conn, DENIED, sql, c=b, **good)  # another tenant's row
        assert conn.execute(text("UPDATE fx_rates SET rate = 1 WHERE id IN (:p, :f)"),
                            {"p": platform, "f": foreign}).rowcount == 0
        own = fx_rate(conn, a, status="PENDING_APPROVAL")
        expect_error(conn, DENIED, "UPDATE fx_rates SET company_id = NULL WHERE id = :i", i=own)
        expect_error(conn, DENIED, "UPDATE fx_rates SET company_id = :b WHERE id = :i", b=b, i=own)
        expect_error(conn, DENIED, "DELETE FROM fx_rates WHERE id = :i", i=own)
        # Zero, negative and same-currency rates are refused (EC-MON-07).
        for bad in ({"rate": "0"}, {"rate": "-1.5"}, {"quote": "USD"}, {"base": "usd"}):
            expect_error(conn, CHECK, sql, c=a, **{**good, **bad})
        expect_error(conn, CHECK, "UPDATE fx_rates SET rate_type = 'SPOT' WHERE id = :i", i=own)
        expect_error(conn, CHECK, "UPDATE fx_rates SET status = 'APPROVED' WHERE id = :i", i=own)
        user = admin_user_id(a)
        expect_error(conn, CHECK, "UPDATE fx_rates SET created_by_user_id = :u, approved_by_user_id = :u "
                     "WHERE id = :i", u=user, i=own)
        conn.execute(text("UPDATE fx_rates SET status = 'ACTIVE' WHERE id = :i"), {"i": own})
    with owner() as conn:
        assert scalar(conn, "SELECT rate FROM fx_rates WHERE id = :i", i=platform) > 1
        assert scalar(conn, "SELECT company_id FROM fx_rates WHERE id = :i", i=foreign) == b


def test_ec_mon_09_snapshots_cite_only_usable_rates_and_keep_their_copy(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with owner() as conn:
        platform = fx_rate(conn, None)
        pending_platform = fx_rate(conn, None, status="PENDING_APPROVAL", as_of="2026-10-02")
    with tenant(b) as conn:
        foreign = fx_rate(conn, b)
    sql = ("INSERT INTO fx_snapshot_rates (company_id, snapshot_id, fx_rate_id, base_currency, quote_currency, rate, "
           "rate_type, source, as_of) VALUES (:c, :s, :r, 'USD', :q, 83.25, 'MID', 'ECB', '2026-10-01')")
    conversion = ("INSERT INTO fx_conversions (company_id, source_amount, source_currency, target_currency, purpose, "
                  "fx_rate_id, rate, quotation, rate_date, rounding_mode, result_amount) VALUES (:c, 100, 'USD', "
                  "'INR', :p, :r, 83.25, '1 USD = rate INR', '2026-10-01', :m, 8325)")
    with tenant(a) as conn:
        own = fx_rate(conn, a, rate="0.910000000000", quote="EUR", as_of="2026-10-03")
        snapshot = insert(conn, "fx_snapshots", company_id=a, purpose="RUN", captured_at="2026-10-01T00:00:00Z")
        conn.execute(text(sql), dict(c=a, s=snapshot, r=platform, q="INR"))
        conn.execute(text(sql), dict(c=a, s=snapshot, r=own, q="EUR"))
        expect_error(conn, FOREIGN_KEY, sql, c=a, s=snapshot, r=foreign, q="GBP")  # another tenant's rate
        expect_error(conn, FOREIGN_KEY, sql, c=a, s=snapshot, r=pending_platform, q="AED")  # not approved
        expect_error(conn, UNIQUE, sql, c=a, s=snapshot, r=platform, q="INR")
        conn.execute(text(conversion), dict(c=a, p="PAY_TO_PAYMENT", r=platform, m="HALF_EVEN"))
        conn.execute(text(conversion), dict(c=a, p="REPORTING", r=None, m="HALF_UP"))
        expect_error(conn, FOREIGN_KEY, conversion, c=a, p="REPORTING", r=foreign, m="HALF_UP")
        expect_error(conn, CHECK, conversion, c=a, p="SPECULATION", r=platform, m="HALF_UP")
        expect_error(conn, CHECK, conversion, c=a, p="REPORTING", r=platform, m="BANKERS")
        # Later changes to the rate never alter the frozen copy.
        conn.execute(text("UPDATE fx_rates SET rate = 90 WHERE id = :i"), {"i": own})
        assert scalar(conn, "SELECT rate FROM fx_snapshot_rates WHERE fx_rate_id = :i", i=own) == Decimal("83.25")


# ---------------------------------------------------------------------------
# Jobs, idempotency keys, outbox
# ---------------------------------------------------------------------------
JOB_SQL = "INSERT INTO jobs (company_id, type, idempotency_key) VALUES (:c, :t, :k) RETURNING id"


def test_ec_run_02_job_idempotency_key_is_unique_per_company_and_type(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    with tenant(a) as conn:
        first = scalar(conn, JOB_SQL, c=a, t="payroll.calculate_run", k="run-1")
        expect_error(conn, UNIQUE, JOB_SQL, c=a, t="payroll.calculate_run", k="run-1")
        scalar(conn, JOB_SQL, c=a, t="payroll.generate_documents", k="run-1")
        scalar(conn, JOB_SQL, c=b, t="payroll.calculate_run", k="run-1")
        scalar(conn, JOB_SQL, c=None, t="fx.fetch_rates", k="2026-10-01")
        expect_error(conn, UNIQUE, JOB_SQL, c=None, t="fx.fetch_rates", k="2026-10-01")  # NULL company too
        for _ in range(2):
            scalar(conn, JOB_SQL, c=a, t="payroll.calculate_run", k=None)
        row = conn.execute(text("SELECT status, priority, attempts, max_attempts, progress_done, payload "
                                "FROM jobs WHERE id = :i"), {"i": first}).one()
        assert tuple(row) == ("QUEUED", 100, 0, 5, 0, {})
    with owner() as conn:
        definition = scalar(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_queue'")
        assert "(status, run_at, priority)" in definition and "'QUEUED'" in definition


def test_jobs_status_lock_and_parent_rules(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    child = "INSERT INTO jobs (company_id, type, parent_job_id) VALUES (:c, 'payroll.calculate_chunk', :p) RETURNING id"
    with tenant(a) as conn:
        parent_a = scalar(conn, JOB_SQL, c=a, t="payroll.calculate_run", k=None)
        parent_b = scalar(conn, JOB_SQL, c=b, t="payroll.calculate_run", k=None)
        platform = scalar(conn, JOB_SQL, c=None, t="fx.fetch_rates", k=None)
        scalar(conn, child, c=a, p=parent_a)
        scalar(conn, child, c=a, p=platform)
        expect_error(conn, FOREIGN_KEY, child, c=a, p=parent_b)
        expect_error(conn, CHECK, "UPDATE jobs SET status = 'RUNNING' WHERE id = :i", i=parent_a)
        conn.execute(text("UPDATE jobs SET status = 'RUNNING', locked_by = 'worker-1', locked_at = now(), "
                          "heartbeat_at = now(), attempts = 1 WHERE id = :i"), {"i": parent_a})
        expect_error(conn, CHECK, "UPDATE jobs SET status = 'PAUSED' WHERE id = :i", i=parent_a)
        expect_error(conn, CHECK, "UPDATE jobs SET max_attempts = 0 WHERE id = :i", i=parent_a)
        expect_error(conn, CHECK, "UPDATE jobs SET payload = '[]' WHERE id = :i", i=parent_a)
        expect_error(conn, CHECK, "UPDATE jobs SET parent_job_id = id WHERE id = :i", i=parent_a)
        expect_error(conn, DENIED, "DELETE FROM jobs WHERE id = :i", i=platform)


def test_ec_run_04_idempotency_keys_are_unique_per_user(three_companies, app):
    a_admin = three_companies["A"]
    a = a_admin.company_id
    admin = admin_user_id(a)
    other = invite_and_activate(app, a_admin, create_employee(a_admin, "Ivy"), ["hr_admin"]).user_id
    sql = ("INSERT INTO idempotency_keys (company_id, user_id, key, method, path, request_hash, expires_at) "
           "VALUES (:c, :u, :k, :m, '/api/v1/payroll/runs/1/finalize', :h, now() + interval '1 day')")
    with tenant(a) as conn:
        conn.execute(text(sql), dict(c=a, u=admin, k="finalize-1", m="POST", h=HASH))
        expect_error(conn, UNIQUE, sql, c=a, u=admin, k="finalize-1", m="POST", h=OTHER_HASH)
        conn.execute(text(sql), dict(c=a, u=other, k="finalize-1", m="POST", h=HASH))
        expect_error(conn, CHECK, sql, c=a, u=admin, k="get-1", m="GET", h=HASH)
        expect_error(conn, CHECK, sql, c=a, u=admin, k="short-hash", m="POST", h="abc")
        expect_error(conn, CHECK, "UPDATE idempotency_keys SET status_code = 42 WHERE key = 'finalize-1'")
        conn.execute(text("UPDATE idempotency_keys SET status_code = 200, response = '{\"id\": 1}' "
                          "WHERE user_id = :u"), {"u": admin})


def test_ec_rel_02_outbox_events_pending_index(three_companies):
    a = three_companies["A"].company_id
    with tenant(a) as conn:
        event = insert(conn, "outbox_events", company_id=a, topic="payroll.payslips.published",
                       payload='{"run_id": 1}')
        assert scalar(conn, "SELECT count(*) FROM outbox_events WHERE dispatched_at IS NULL") == 1
        conn.execute(text("UPDATE outbox_events SET dispatched_at = now(), attempts = 1 WHERE id = :i"), {"i": event})
        expect_error(conn, CHECK, "UPDATE outbox_events SET attempts = -1 WHERE id = :i", i=event)
        expect_error(conn, CHECK, "UPDATE outbox_events SET payload = '\"x\"' WHERE id = :i", i=event)
    with owner() as conn:
        definition = scalar(conn, "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_outbox_events_pending'")
        assert "dispatched_at IS NULL" in definition


# ---------------------------------------------------------------------------
# Generic approvals, chains and delegations
# ---------------------------------------------------------------------------
REQUEST_SQL = ("INSERT INTO approval_requests (company_id, module, subject_type, subject_id, amount, currency) "
               "VALUES (:c, :m, 'overtime_entry', :s, :a, :cur) RETURNING id")


def test_approval_requests_one_pending_per_subject(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    base = dict(m="overtime", s=42, a=None, cur=None)
    with tenant(a) as conn:
        first = scalar(conn, REQUEST_SQL, c=a, **base)
        expect_error(conn, UNIQUE, REQUEST_SQL, c=a, **base)
        scalar(conn, REQUEST_SQL, c=a, **{**base, "m": "task"})
        expect_error(conn, CHECK, "UPDATE approval_requests SET status = 'APPROVED' WHERE id = :i", i=first)
        conn.execute(text("UPDATE approval_requests SET status = 'APPROVED', decided_at = now() WHERE id = :i"),
                     {"i": first})
        scalar(conn, REQUEST_SQL, c=a, **base)  # a decided request no longer blocks a new one
        expect_error(conn, CHECK, REQUEST_SQL, c=a, **{**base, "m": "payroll", "s": 1})
        expect_error(conn, CHECK, REQUEST_SQL, c=a, **{**base, "s": 2, "a": "100.00"})  # amount without currency
        scalar(conn, REQUEST_SQL, c=a, **{**base, "m": "reimbursement", "s": 3, "a": "100.00", "cur": "INR"})
        expect_error(conn, CHECK, "UPDATE approval_requests SET context = '[]' WHERE id = :i", i=first)
    with tenant(b) as conn:
        scalar(conn, REQUEST_SQL, c=b, **base)


def test_approval_request_steps_mirror_leave_approvals(three_companies):
    a = three_companies["A"].company_id
    user = admin_user_id(a)
    sql = ("INSERT INTO approval_request_steps (company_id, request_id, step_order, approver_type, specific_user_id, "
           "status, acted_as) VALUES (:c, :r, :o, :t, :u, :s, :as_)")
    with tenant(a) as conn:
        request = scalar(conn, REQUEST_SQL, c=a, m="bank_change", s=1, a=None, cur=None)
        conn.execute(text(sql), dict(c=a, r=request, o=1, t="primary_manager", u=None, s="approved", as_="delegate"))
        conn.execute(text(sql), dict(c=a, r=request, o=2, t="specific_user", u=user, s="pending", as_=None))
        conn.execute(text(sql), dict(c=a, r=request, o=3, t="finance_role", u=None, s="waiting", as_=None))
        expect_error(conn, UNIQUE, sql, c=a, r=request, o=3, t="finance_role", u=None, s="waiting", as_=None)
        expect_error(conn, CHECK, sql, c=a, r=request, o=4, t="specific_user", u=None, s="waiting", as_=None)
        expect_error(conn, CHECK, sql, c=a, r=request, o=4, t="role", u=None, s="waiting", as_=None)
        expect_error(conn, CHECK, sql, c=a, r=request, o=4, t="ceo", u=None, s="waiting", as_=None)
        expect_error(conn, CHECK, sql, c=a, r=request, o=4, t="finance_role", u=None, s="done", as_=None)
        expect_error(conn, CHECK, sql, c=a, r=request, o=4, t="finance_role", u=None, s="waiting", as_="boss")
        expect_error(conn, CHECK, sql, c=a, r=request, o=0, t="finance_role", u=None, s="waiting", as_=None)
        conn.execute(text("DELETE FROM approval_requests WHERE id = :i"), {"i": request})
        assert scalar(conn, "SELECT count(*) FROM approval_request_steps") == 0  # cascades


CHAIN_SQL = ("INSERT INTO approval_chain_steps (company_id, module, step_order, approver_type, role_id, "
             "specific_user_id, min_days, min_amount, min_amount_currency, condition, created_at, updated_at) "
             "VALUES (:c, :m, :o, :t, NULL, :u, :d, :amt, :cur, :cond, now(), now())")


def _chain(**overrides):
    values = dict(m="payroll_run", o=1, t="finance_role", u=None, d=None, amt=None, cur=None, cond=None)
    values.update(overrides)
    return values


def test_approval_chain_steps_accept_payroll_modules_and_approver_types(three_companies):
    a, b = three_companies["A"].company_id, three_companies["B"].company_id
    user = admin_user_id(a)
    with tenant(a) as conn:
        for module in _migration().APPROVAL_MODULES:
            if module != "leave":  # the signup chain already uses leave steps 1 and 2
                conn.execute(text(CHAIN_SQL), dict(c=a, **_chain(m=module, o=10)))
        conn.execute(text(CHAIN_SQL), dict(c=a, **_chain(o=11, t="specific_user", u=user)))
        conn.execute(text(CHAIN_SQL), dict(c=a, **_chain(o=12, amt="50000.0000", cur="INR",
                                                         cond='{"min_net_change_pct": "10"}')))
        conn.execute(text(CHAIN_SQL), dict(c=a, **_chain(m="leave", o=9, t="skip_level_manager", d=5)))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(m="payroll", o=20))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(m="unknown", o=20))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, t="ceo"))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, t="specific_user"))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, u=user))  # a user only on specific_user steps
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, amt="10"))  # money without currency
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, amt="-1", cur="INR"))
        expect_error(conn, CHECK, CHAIN_SQL, c=a, **_chain(o=20, amt="1", cur="inr"))
        expect_error(conn, UNIQUE, CHAIN_SQL, c=a, **_chain(o=10))
    with owner() as conn:  # a specific user must belong to the chain's company
        expect_error(conn, FOREIGN_KEY, CHAIN_SQL, c=b, **_chain(o=11, t="specific_user", u=user))


def test_approval_delegations_accept_the_same_modules(three_companies):
    a_admin = three_companies["A"]
    a = a_admin.company_id
    one, two = create_employee(a_admin, "Dana")["id"], create_employee(a_admin, "Eli")["id"]
    sql = ("INSERT INTO approval_delegations (company_id, delegator_employee_id, delegate_employee_id, module, "
           "starts_on, ends_on, created_at, updated_at) VALUES (:c, :f, :t, :m, '2026-10-01', '2026-10-31', now(), now())")
    with tenant(a) as conn:
        for module in ("leave", "payroll_run", "overtime", "bank_change", "settings"):
            conn.execute(text(sql), dict(c=a, f=one, t=two, m=module))
        expect_error(conn, CHECK, sql, c=a, f=one, t=two, m="payroll")
        expect_error(conn, CHECK, sql, c=a, f=one, t=two, m="unknown")


def test_leave_chains_keep_working_beside_payroll_chains(org, app):
    users, emp = org["users"], org["emp"]
    admin = users["admin_a"]
    cid = admin.me()["company"]["id"]
    count = "SELECT count(*) FROM approval_chain_steps WHERE module <> 'leave'"
    with tenant(cid) as conn:
        before = scalar(conn, count)
        conn.execute(text(CHAIN_SQL), dict(c=cid, **_chain(o=31)))
        conn.execute(text(CHAIN_SQL), dict(c=cid, **_chain(m="overtime", o=31, t="primary_manager")))
    chain = admin.get("/api/v1/leaves/approval-chain")
    assert chain.status_code == 200, chain.text
    assert [s["approver_type"] for s in chain.json()] == ["primary_manager", "role"]
    # The leave API still offers only the leave approver types.
    bad = admin.put("/api/v1/leaves/approval-chain", json={"steps": [{"approver_type": "finance_role"}]})
    assert bad.status_code == 422
    saved = admin.put("/api/v1/leaves/approval-chain", json={"steps": [{"approver_type": "primary_manager"}]})
    assert saved.status_code == 200, saved.text
    leave = users["e1"].post("/api/v1/leaves/apply", json={"leave_type": "casual", "start_date": "2099-06-01",
                                                          "end_date": "2099-06-03"}).json()
    assert [s["approver_type"] for s in leave["steps"]] == ["primary_manager"]
    assert leave["steps"][0]["approver"] == emp["m2"]["full_name"]
    decided = users["m2"].post(f"/api/v1/leaves/{leave['id']}/approve", json={})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"
    with tenant(cid) as conn:  # saving the leave chain left the payroll chains alone
        assert scalar(conn, count) == before + 2


# ---------------------------------------------------------------------------
# Grants for fresh databases
# ---------------------------------------------------------------------------
def test_fresh_database_grant_app_role_covers_platform_tables():
    from sqlalchemy import create_engine

    from app.cli import grant_app_role
    from app.database.db import system_engine

    role = "hrms_grant_probe"
    probe = create_engine(system_engine.url, isolation_level="AUTOCOMMIT")
    try:
        with probe.connect() as conn:
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
            conn.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
            grant_app_role(conn, role)
            granted = {
                (table, privilege): conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                                 {"r": role, "t": table, "p": privilege}).scalar()
                for table, privileges in _migration().PLATFORM_GRANTS
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            expected = {(table, privilege): privilege in privileges
                        for table, privileges in _migration().PLATFORM_GRANTS
                        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")}
            conn.execute(text(f"DROP OWNED BY {role}"))
            conn.execute(text(f"DROP ROLE {role}"))
    finally:
        probe.dispose()
    assert granted == expected
