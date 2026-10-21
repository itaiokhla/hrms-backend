"""Half-day leave, leave_days, balance transactions and approved-leave cancellation (3.4 §7.1; 3.1 §5.3; audit 03 §8.1-6).

EC-LV-04, EC-LV-06, EC-LV-07, EC-LV-10, EC-LV-11, EC-LV-13.
2099-06-01 is a Monday; tenants start with the MON_FRI work week.
"""

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database.db import SessionLocal, bind_tenant, system_engine
from tests.helpers import create_employee, invite_and_activate, signup_company

LEAVES = "/api/v1/leaves"


def _sql(query, **params):
    with system_engine.begin() as conn:
        result = conn.execute(text(query), params)
        return [dict(r._mapping) for r in result] if result.returns_rows else None


def _set_setting(company_id, key, value):
    payload = json.dumps(value)
    updated = _sql(
        "UPDATE setting_values SET value = CAST(:v AS jsonb) WHERE company_id = :c AND key = :k "
        "AND scope_type = 'TENANT' AND status = 'ACTIVE' RETURNING id", c=company_id, k=key, v=payload)
    if not updated:
        _sql(
            "INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
            "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', NULL, CAST(:v AS jsonb), false, "
            "'2000-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=company_id, k=key, v=payload)


def _apply(client, start, end=None, leave_type="casual", **portions):
    return client.post(f"{LEAVES}/apply", json={"leave_type": leave_type, "start_date": start,
                                                "end_date": end or start, **portions})


def _approve(client, leave_id):
    r = client.post(f"{LEAVES}/{leave_id}/approve", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _leave_days(leave_id):
    return _sql("SELECT date, portion, units, counts_against_balance, voided_at, pay_treatment_snapshot "
                "FROM leave_days WHERE leave_id = :l ORDER BY date, portion", l=leave_id)


def _balance(client, code="casual", year=2099):
    r = client.get(f"{LEAVES}/my/balance", params={"year": year})
    assert r.status_code == 200, r.text
    return r.json()[code]


@pytest.fixture
def team(app):
    admin = signup_company(app, "Acme Corp")
    boss = create_employee(admin, "Boss")
    worker = create_employee(admin, "Worker", manager_id=boss["id"])
    return {
        "admin": admin, "cid": admin.company_id,
        "boss": invite_and_activate(app, admin, boss, ["manager"]),
        "worker": invite_and_activate(app, admin, worker, ["employee"]),
        "worker_id": worker["id"],
    }


def test_half_day_leave_generates_leave_days_and_charges_half(team):
    w, boss = team["worker"], team["boss"]
    r = _apply(w, "2099-06-01", "2099-06-02", end_portion="FIRST_HALF")
    assert r.status_code == 200, r.text
    leave = r.json()
    assert leave["number_of_days"] == 1.5 and leave["end_portion"] == "FIRST_HALF"
    assert _balance(w)["pending"] == 1.5
    assert _leave_days(leave["id"]) == []  # generated at approval only
    done = _approve(boss, leave["id"])
    assert done["status"] == "approved"
    rows = _leave_days(leave["id"])
    assert [(r["date"], r["portion"], r["units"]) for r in rows] == [
        (date(2099, 6, 1), "FULL", Decimal("1.00")), (date(2099, 6, 2), "FIRST_HALF", Decimal("0.50"))]
    assert all(r["counts_against_balance"] and r["voided_at"] is None for r in rows)
    assert rows[0]["pay_treatment_snapshot"]["pay_treatment"] == "FULL"
    balance = _balance(w)
    assert balance["used"] == 1.5 and balance["remaining"] == 8.5 and balance["pending"] == 0
    tx = _sql("SELECT delta, reason, idempotency_key FROM leave_balance_transactions WHERE ref_id = :l", l=leave["id"])
    assert tx == [{"delta": Decimal("-1.50"), "reason": "USE", "idempotency_key": f"leave:{leave['id']}:use:2099"}]
    log = _sql("SELECT record_type, change, employee_id FROM source_change_log WHERE record_type = 'leave' "
               "AND record_id = :l", l=leave["id"])
    assert log == [{"record_type": "leave", "change": "CREATED", "employee_id": team["worker_id"]}]


def test_whole_days_still_serialise_as_integers(team):
    leave = _apply(team["worker"], "2099-06-01", "2099-06-03").json()
    assert leave["number_of_days"] == 3 and isinstance(leave["number_of_days"], int)
    assert leave["start_portion"] == "FULL" and leave["cancellation_status"] is None


def test_two_halves_of_one_day_and_overlap_rules(team):
    w = team["worker"]
    pm = _apply(w, "2099-06-03", start_portion="SECOND_HALF")
    assert pm.status_code == 200, pm.text
    assert pm.json()["number_of_days"] == 0.5
    am = _apply(w, "2099-06-03", end_portion="FIRST_HALF")
    assert am.status_code == 200, am.text
    assert _apply(w, "2099-06-03").status_code == 409
    assert _apply(w, "2099-06-03", end_portion="FIRST_HALF").status_code == 409
    _approve(team["boss"], pm.json()["id"])
    _approve(team["boss"], am.json()["id"])
    assert _balance(w)["used"] == 1


def test_invalid_portions_are_rejected(team):
    w = team["worker"]
    r = _apply(w, "2099-06-03", start_portion="SECOND_HALF", end_portion="FIRST_HALF")
    assert r.status_code == 400 and r.json()["code"] == "INVALID_PORTION"
    assert _apply(w, "2099-06-03", start_portion="FIRST_HALF").status_code == 422
    assert _apply(w, "2099-06-03", end_portion="MORNING").status_code == 422


def test_half_day_switch_off_allows_only_whole_days(team):
    _set_setting(team["cid"], "leave.half_day.enabled", False)
    w = team["worker"]
    r = _apply(w, "2099-06-03", end_portion="FIRST_HALF")
    assert r.status_code == 400 and r.json()["code"] == "HALF_DAY_DISABLED"
    assert _apply(w, "2099-06-03").status_code == 200


def test_leave_type_without_half_days(team):
    _sql("UPDATE leave_types SET allow_half_day = false WHERE company_id = :c AND code = 'earned'", c=team["cid"])
    r = _apply(team["worker"], "2099-06-03", leave_type="earned", end_portion="FIRST_HALF")
    assert r.status_code == 400 and r.json()["code"] == "HALF_DAY_NOT_ALLOWED"


def test_EC_LV_10_leave_across_a_weekly_off_charges_working_days_only(team):
    w = team["worker"]
    leave = _apply(w, "2099-06-05", "2099-06-08").json()  # Friday to Monday
    assert leave["number_of_days"] == 2
    _approve(team["boss"], leave["id"])
    rows = _leave_days(leave["id"])
    assert [(r["date"].weekday(), r["counts_against_balance"]) for r in rows] == [
        (4, True), (5, False), (6, False), (0, True)]
    assert _balance(w)["used"] == 2


def test_EC_LV_10_leave_type_counting_non_working_days(team):
    _sql("UPDATE leave_types SET counts_non_working_days = true WHERE company_id = :c AND code = 'earned'", c=team["cid"])
    leave = _apply(team["worker"], "2099-06-05", "2099-06-08", leave_type="earned").json()
    assert leave["number_of_days"] == 4
    _approve(team["boss"], leave["id"])
    assert all(r["counts_against_balance"] for r in _leave_days(leave["id"]))
    assert _leave_days(leave["id"])[0]["pay_treatment_snapshot"]["counts_non_working_days"] is True


def test_EC_LV_06_half_day_leave_on_a_holiday_consumes_nothing(team):
    admin, w = team["admin"], team["worker"]
    assert admin.post("/api/v1/holidays", json={"name": "Founders Day", "date": "2099-06-10"}).status_code == 200
    r = _apply(w, "2099-06-10", end_portion="FIRST_HALF")
    assert r.status_code == 400 and r.json()["code"] == "NO_WORKING_DAYS"
    leave = _apply(w, "2099-06-09", "2099-06-11").json()
    assert leave["number_of_days"] == 2
    _approve(team["boss"], leave["id"])
    assert [r["counts_against_balance"] for r in _leave_days(leave["id"])] == [True, False, True]


def test_EC_LV_06_holiday_added_after_approval_gives_the_day_back(team):
    admin, w = team["admin"], team["worker"]
    leave = _apply(w, "2099-06-15", "2099-06-17").json()
    _approve(team["boss"], leave["id"])
    assert _balance(w)["used"] == 3
    r = admin.post("/api/v1/holidays", json={"name": "Surprise Day", "date": "2099-06-16"})
    assert r.status_code == 200, r.text
    assert _balance(w)["used"] == 2
    assert [r["counts_against_balance"] for r in _leave_days(leave["id"])] == [True, False, True]
    assert w.get(f"{LEAVES}/my").json()[0]["number_of_days"] == 2
    changes = _sql("SELECT change, details FROM source_change_log WHERE record_type = 'leave' AND record_id = :l "
                   "ORDER BY id", l=leave["id"])
    assert [c["change"] for c in changes] == ["CREATED", "UPDATED"]
    assert changes[1]["details"] == {"reason": "HOLIDAY_CHANGED"}
    # Removing the holiday charges the day again, through a new idempotent transaction.
    holiday_id = r.json()["id"]
    assert admin.delete(f"/api/v1/holidays/{holiday_id}").status_code == 200
    assert _balance(w)["used"] == 3


def test_EC_LV_13_leave_after_the_exit_date_is_rejected(team):
    _sql("UPDATE employees SET termination_date = '2099-06-30' WHERE id = :e", e=team["worker_id"])
    w = team["worker"]
    r = _apply(w, "2099-06-29", "2099-07-01")
    assert r.status_code == 400 and r.json()["code"] == "OUTSIDE_EMPLOYMENT"
    assert _apply(w, "2099-06-29", "2099-06-30").status_code == 200


def test_EC_LV_04_cancelling_approved_leave_is_approved_then_voided(team):
    w, boss = team["worker"], team["boss"]
    leave = _apply(w, "2099-06-01", "2099-06-03").json()
    _approve(boss, leave["id"])
    r = w.post(f"{LEAVES}/{leave['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["cancellation_status"] == "pending"
    assert w.post(f"{LEAVES}/{leave['id']}/cancel").status_code == 409
    assert _balance(w)["used"] == 3
    inbox = boss.get(f"{LEAVES}/approvals").json()
    assert [i["id"] for i in inbox] == [leave["id"]] and inbox[0]["can_act"]
    # The requester cannot approve their own cancellation.
    assert w.post(f"{LEAVES}/{leave['id']}/approve", json={}).status_code == 403
    done = _approve(boss, leave["id"])
    assert done["status"] == "cancelled" and done["cancellation_status"] == "approved"
    assert all(r["voided_at"] is not None for r in _leave_days(leave["id"]))
    assert _balance(w)["used"] == 0
    keys = sorted(t["idempotency_key"] for t in _sql(
        "SELECT idempotency_key FROM leave_balance_transactions WHERE ref_id = :l", l=leave["id"]))
    assert keys == [f"leave:{leave['id']}:cancel:2099", f"leave:{leave['id']}:use:2099"]
    assert [c["change"] for c in _sql("SELECT change FROM source_change_log WHERE record_type = 'leave' "
                                      "AND record_id = :l ORDER BY id", l=leave["id"])] == ["CREATED", "VOIDED"]
    # The same dates can be booked again.
    assert _apply(w, "2099-06-02").status_code == 200


def test_EC_LV_04_rejected_cancellation_keeps_the_leave(team):
    w, boss = team["worker"], team["boss"]
    leave = _apply(w, "2099-06-01").json()
    _approve(boss, leave["id"])
    w.post(f"{LEAVES}/{leave['id']}/cancel")
    r = boss.post(f"{LEAVES}/{leave['id']}/reject", json={"remarks": "needed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["cancellation_status"] == "rejected"
    assert _balance(w)["used"] == 1
    assert all(r["voided_at"] is None for r in _leave_days(leave["id"]))


def test_cancellation_out_of_scope_is_not_found(team, app):
    other = signup_company(app, "Beta Industries")
    w = team["worker"]
    leave = _apply(w, "2099-06-01").json()
    _approve(team["boss"], leave["id"])
    w.post(f"{LEAVES}/{leave['id']}/cancel")
    assert other.post(f"{LEAVES}/{leave['id']}/approve", json={}).status_code == 404
    assert other.post(f"{LEAVES}/{leave['id']}/cancel").status_code == 404


def test_EC_LV_07_balance_transactions_are_idempotent_and_append_only(team):
    from app.payroll.services import time_inputs

    cid, eid = team["cid"], team["worker_id"]
    db = SessionLocal()
    try:
        bind_tenant(db, cid)
        kwargs = dict(employee_id=eid, leave_type="earned", delta=Decimal("2"), reason="ADJUST", key="test:adjust:1",
                      effective_date=date(2099, 1, 1), ref_type="test", ref_id=1, actor_user_id=None)
        assert time_inputs._transaction(db, cid, **kwargs) is True
        assert time_inputs._transaction(db, cid, **kwargs) is False
        db.commit()
        with pytest.raises(Exception):
            db.execute(text("UPDATE leave_balance_transactions SET delta = 5"))
            db.flush()
        db.rollback()
    finally:
        db.close()
    assert _balance(team["worker"], "earned")["remaining"] == 17


def test_EC_LV_07_regenerating_leave_days_is_a_no_op(team):
    from app.models.leave import Leave
    from app.payroll.services import time_inputs

    leave = _apply(team["worker"], "2099-06-01", "2099-06-02").json()
    _approve(team["boss"], leave["id"])
    db = SessionLocal()
    try:
        bind_tenant(db, team["cid"])
        time_inputs.generate_leave_days(db, db.get(Leave, leave["id"]))
        db.commit()
    finally:
        db.close()
    assert len(_leave_days(leave["id"])) == 2
    assert _balance(team["worker"])["used"] == 2


def test_EC_LV_11_leave_type_code_is_frozen_once_used(team):
    admin = team["admin"]
    types = {t["code"]: t for t in admin.get(f"{LEAVES}/types").json()}
    casual = types["casual"]
    body = {k: casual[k] for k in ("name", "annual_quota", "is_paid", "is_unlimited", "is_active")}
    assert admin.put(f"{LEAVES}/types/{casual['id']}", json={**body, "code": "casual2"}).status_code == 200
    _apply(team["worker"], "2099-06-01", leave_type="casual2")
    r = admin.put(f"{LEAVES}/types/{casual['id']}", json={**body, "code": "casual3"})
    assert r.status_code == 409 and r.json()["code"] == "LEAVE_TYPE_CODE_FROZEN"
    assert admin.put(f"{LEAVES}/types/{casual['id']}", json={**body, "code": "casual2", "name": "Casual"}).status_code == 200


def test_migration_backfill_turns_legacy_approved_leave_into_leave_days(team):
    """0016 backfill: legacy approved leave gets one FULL day per calendar date and one USE transaction, once."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0016_time_inputs.py"
    spec = importlib.util.spec_from_file_location("migration_0016", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    leave_id = _sql(
        "INSERT INTO leaves (company_id, employee_id, leave_type, start_date, end_date, number_of_days, status, "
        "created_at, updated_at) VALUES (:c, :e, 'casual', '2099-08-01', '2099-08-03', 3, 'approved', now(), now()) "
        "RETURNING id", c=team["cid"], e=team["worker_id"])[0]["id"]
    with system_engine.begin() as conn:
        first = migration.backfill(conn)
        second = migration.backfill(conn)
    assert first == {"leave_days": 3, "balance_transactions": 1}
    assert second == {"leave_days": 0, "balance_transactions": 0}
    rows = _leave_days(leave_id)
    assert [(r["portion"], r["counts_against_balance"]) for r in rows] == [("FULL", True)] * 3
    assert rows[0]["pay_treatment_snapshot"]["source"] == "LEGACY_MIGRATION"
    assert _balance(team["worker"])["used"] == 3


def test_migration_0016_upgrade_downgrade_and_upgrade_again_with_legacy_data(app):
    """Rollback rehearsal on a scratch database: legacy leave, shifts and holidays survive 0016 both ways."""
    import os

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import DBAPIError

    from tests.payroll.test_org_schema import _alembic, _seed

    owner_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
    scratch = "hrms_test_m0016"
    admin = create_engine(owner_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
            conn.execute(text(f"CREATE DATABASE {scratch}"))
    except DBAPIError:
        admin.dispose()
        pytest.skip("the test server does not allow creating a scratch database")
    url = owner_url.set(database=scratch).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        _alembic(url, "upgrade", "0015_compensation_v2")
        with engine.begin() as conn:
            cid = _seed(conn, "companies", name="Legacy Co", slug="legacy-co", status="active", currency="INR",
                        timezone="Asia/Kolkata", subscription_status="active", subscription_plan="trial")
            shift = _seed(conn, "shifts", company_id=cid, name="Day", start_time="09:00", end_time="18:00",
                          duration_hours=9, is_flexible=False, is_active=True)
            employee = _seed(conn, "employees", company_id=cid, employee_code="E1", first_name="Asha", last_name="Rao",
                             email="asha@legacy.example", hire_date=date(2024, 1, 1), shift_id=shift)
            _seed(conn, "leave_types", company_id=cid, code="casual", name="Casual", annual_quota=10, is_paid=True,
                  is_unlimited=False, is_active=True)
            leave = _seed(conn, "leaves", company_id=cid, employee_id=employee, leave_type="casual",
                          start_date=date(2026, 3, 6), end_date=date(2026, 3, 8), number_of_days=3, status="approved")
            _seed(conn, "leaves", company_id=cid, employee_id=employee, leave_type="casual",
                  start_date=date(2026, 4, 1), end_date=date(2026, 4, 1), number_of_days=1, status="pending")

        _alembic(url, "upgrade", "0016_time_inputs")
        with engine.begin() as conn:
            days = conn.execute(text("SELECT date, portion, counts_against_balance FROM leave_days WHERE leave_id = :l "
                                     "ORDER BY date"), {"l": leave}).fetchall()
            assert [(d.isoformat(), p, c) for d, p, c in days] == [
                ("2026-03-06", "FULL", True), ("2026-03-07", "FULL", True), ("2026-03-08", "FULL", True)]
            tx = conn.execute(text("SELECT delta, idempotency_key FROM leave_balance_transactions")).fetchall()
            assert [tuple(t) for t in tx] == [(Decimal("-3.00"), f"leave:{leave}:use:2026")]
            assert conn.execute(text("SELECT duration_hours FROM shifts")).scalar() == Decimal("9.00")
            conn.execute(text("UPDATE leaves SET number_of_days = 0.5, end_portion = 'FIRST_HALF' WHERE status = 'pending'"))
            conn.execute(text("UPDATE shifts SET duration_hours = 8.5"))

        _alembic(url, "downgrade", "0015_compensation_v2")
        inspector = inspect(engine)
        assert not {"leave_days", "leave_balance_transactions", "lop_inputs", "source_change_log"} & set(
            inspector.get_table_names())
        assert "start_portion" not in {c["name"] for c in inspector.get_columns("leaves")}
        assert "early_exit" not in {c["name"] for c in inspector.get_columns("attendance")}
        with engine.begin() as conn:
            assert sorted(conn.execute(text("SELECT number_of_days FROM leaves")).scalars().all()) == [1, 3]
            assert conn.execute(text("SELECT duration_hours FROM shifts")).scalar() == 9

        _alembic(url, "upgrade", "0016_time_inputs")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM leave_days")).scalar() == 3
            assert conn.execute(text("SELECT count(*) FROM leave_balance_transactions")).scalar() == 1
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :d"), {"d": scratch})
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
        admin.dispose()
