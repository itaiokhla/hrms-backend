"""Shift versions (AS-M1 WI-AS-1.1, 1.3, 1.8; 03-shifts/01 §3; AS 11 §4.4).

Guard (A-24): a version starts no earlier than the owner's local today; tiling without gaps or overlaps; a scheduled
version at the same start is replaced; a temporary change gets a return copy; an explicit end into a later scheduled
version is refused unless truncated (EC AS-SHF-02); dry runs write nothing; the legacy PUT posts a version from the
owner's local tomorrow (A-24) and the legacy POST creates version 1 (BUG-AS-23, -22); new tenants get GEN with a
60-minute unpaid lunch (BUG-AS-33); the migration-shaped version 1 starts 2000-01-01 (AS-MIG-08).
"""

from datetime import date, datetime, timedelta, timezone

import pytest
import pytz
from sqlalchemy import text

from app.database.db import system_engine
from tests.helpers import create_employee, signup_company

IST = pytz.timezone("Asia/Kolkata")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


@pytest.fixture
def admin(app):
    return signup_company(app, "Shift Works")


def _shift(client, code="DAY", name=None, start="09:00", end="17:00", **version):
    body = {"code": code, "name": name or code.title(), "kind": "FIXED",
            "version": {"start_time": start, "end_time": end, **version}}
    r = client.post("/api/v1/shifts", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _versions(client, shift_id):
    r = client.get(f"/api/v1/shifts/{shift_id}/versions")
    assert r.status_code == 200, r.text
    return [v for v in r.json() if v["voided_at"] is None]


def _post_version(client, shift_id, status=201, **body):
    r = client.post(f"/api/v1/shifts/{shift_id}/versions", json=body)
    assert r.status_code == status, r.text
    return r.json()


def test_bug_as_33_new_company_gets_gen_with_an_unpaid_lunch(admin):
    shifts = admin.get("/api/v1/shifts", params={"status": "ALL"}).json()["items"]
    gen = next(s for s in shifts if s["code"] == "GEN")
    assert (gen["name"], gen["start_time"], gen["end_time"]) == ("General Shift", "09:00", "18:00")
    assert gen["unpaid_break_minutes"] == 60
    version = gen["current_version"]
    assert version["effective_from"] == "2000-01-01" and version["effective_to"] is None
    assert version["break_rules"] == [{"kind": "LUNCH", "mode": "FIXED_WINDOW", "minutes": 60, "paid": False,
                                       "start": "13:00", "end": "14:00"}]


def test_as_mig_08_legacy_post_creates_version_1_and_keeps_the_legacy_shape(admin):
    r = admin.post("/api/v1/shifts", json={"name": "Night Watch", "start_time": "22:00", "end_time": "06:00",
                                           "duration_hours": "8.00", "unpaid_break_minutes": 30})
    assert r.status_code == 200, r.text
    legacy = next(s for s in admin.get("/api/v1/shifts").json() if s["id"] == r.json()["id"])
    assert legacy["code"] == "NIGHT_WATCH" and legacy["kind"] == "FIXED"
    assert (legacy["start_time"], legacy["end_time"], legacy["unpaid_break_minutes"]) == ("22:00", "06:00", 30)
    assert str(legacy["duration_hours"]) in ("8.00", "8.0", "8")
    [version] = _versions(admin, legacy["id"])
    assert version["version_no"] == 1 and version["crosses_midnight"] is True
    assert version["effective_from"] == "2000-01-01"  # a legacy shift applies to every date, as before AS-M1


def test_bug_as_23_a_shift_without_duration_gets_the_computed_gross_span(admin):
    r = admin.post("/api/v1/shifts", json={"name": "Short", "start_time": "10:00", "end_time": "14:30"})
    assert r.status_code == 200, r.text
    detail = admin.get(f"/api/v1/shifts/{r.json()['id']}").json()
    assert detail["duration_hours"] is not None and float(detail["duration_hours"]) == 4.5


def test_version_from_tomorrow_tiles_and_leaves_the_mirror_on_today(admin):
    shift = _shift(admin)
    tomorrow = _today() + timedelta(days=1)
    result = _post_version(admin, shift["id"], effective_from=tomorrow.isoformat(), start_time="08:00",
                           end_time="16:00")
    assert result["version"]["version_no"] == 2
    v1, v2 = _versions(admin, shift["id"])
    assert v1["effective_to"] == _today().isoformat() and v2["effective_from"] == tomorrow.isoformat()
    assert v2["effective_to"] is None
    detail = admin.get(f"/api/v1/shifts/{shift['id']}").json()
    assert detail["start_time"] == "09:00" and detail["current_version_id"] == v1["id"]  # mirror = today's version


def test_as_shf_03_a_version_before_the_owner_today_is_refused_with_the_next_allowed_date(admin):
    shift = _shift(admin)
    yesterday = _today() - timedelta(days=1)
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json={"effective_from": yesterday.isoformat(),
                                                                    "start_time": "08:00", "end_time": "16:00"})
    assert r.status_code == 409 and r.json()["code"] == "SHIFT_VERSION_IN_FORCE"
    # The shift's version 1 started today, so the next allowed date is tomorrow.
    assert r.json()["details"]["next_allowed_from"] == (_today() + timedelta(days=1)).isoformat()


def test_a_temporary_change_gets_a_return_copy_of_the_version_it_interrupts(admin):
    shift = _shift(admin, unpaid_break_minutes=30)
    start, end = _today() + timedelta(days=5), _today() + timedelta(days=10)
    result = _post_version(admin, shift["id"], effective_from=start.isoformat(), effective_to=end.isoformat(),
                           start_time="10:00", end_time="16:00")
    assert len(result["created"]) == 2
    v1, temporary, back = _versions(admin, shift["id"])
    assert (v1["effective_to"], temporary["effective_from"], temporary["effective_to"]) == (
        (start - timedelta(days=1)).isoformat(), start.isoformat(), end.isoformat())
    assert back["effective_from"] == (end + timedelta(days=1)).isoformat() and back["effective_to"] is None
    assert back["content_hash"] == v1["content_hash"]  # "return to normal" has the same content


def test_a_scheduled_version_at_the_same_start_is_replaced(admin):
    shift = _shift(admin)
    start = (_today() + timedelta(days=3)).isoformat()
    _post_version(admin, shift["id"], effective_from=start, start_time="08:00", end_time="16:00")
    _post_version(admin, shift["id"], effective_from=start, start_time="07:00", end_time="15:00")
    versions = _versions(admin, shift["id"])
    assert [v["start_time"] for v in versions] == ["09:00", "07:00"]


def test_as_shf_02_an_end_into_a_later_scheduled_version_is_refused_unless_truncated(admin):
    shift = _shift(admin)
    later = _today() + timedelta(days=20)
    _post_version(admin, shift["id"], effective_from=later.isoformat(), start_time="08:00", end_time="16:00")
    body = {"effective_from": (_today() + timedelta(days=10)).isoformat(),
            "effective_to": (_today() + timedelta(days=25)).isoformat(), "start_time": "10:00", "end_time": "16:00"}
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions", json=body)
    assert r.status_code == 409 and r.json()["code"] == "SHIFT_VERSION_OVERLAP_SCHEDULED"
    assert r.json()["details"]["bounding_effective_from"] == later.isoformat()
    result = _post_version(admin, shift["id"], **body, on_overlap="TRUNCATE")
    assert result["warnings"][0]["code"] == "VERSION_RANGE_TRUNCATED"
    assert result["version"]["effective_to"] == (later - timedelta(days=1)).isoformat()
    froms = [v["effective_from"] for v in _versions(admin, shift["id"])]
    assert froms == sorted(froms) and len(froms) == 3  # no return copy when the end was truncated


def test_a_dry_run_previews_without_writing(admin):
    shift = _shift(admin)
    preview = _post_version(admin, shift["id"], status=200, effective_from=(_today() + timedelta(days=2)).isoformat(),
                            start_time="08:00", end_time="18:00", dry_run=True)
    assert preview["dry_run"] is True
    assert [v["new"] for v in preview["versions"]] == [False, True]
    assert preview["scheduled_minutes"] == {"old": 480, "new": 600}
    assert len(_versions(admin, shift["id"])) == 1


def test_invalid_version_bodies_are_422_with_issues(admin):
    shift = _shift(admin)
    r = admin.post(f"/api/v1/shifts/{shift['id']}/versions",
                   json={"effective_from": (_today() + timedelta(days=1)).isoformat(), "start_time": "09:00",
                         "end_time": "09:00"})  # start = end without the long-duty confirm (AS-SHF-08)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION_ERROR"
    assert r.json()["issues"]


def test_guard_and_tiling_triggers(admin):
    """The triggers are the backstop of the service (A-24; AS 11 §4.4)."""
    shift = _shift(admin)
    [v1] = _versions(admin, shift["id"])
    with pytest.raises(Exception) as error:
        with system_engine.begin() as conn:
            conn.execute(text("UPDATE shift_versions SET start_time = '07:00' WHERE id = :id"), {"id": v1["id"]})
    assert "shift_versions_in_force" in str(error.value)
    with pytest.raises(Exception) as error:
        with system_engine.begin() as conn:  # an end with nothing after it leaves the shift without a version
            conn.execute(text("UPDATE shift_versions SET effective_to = effective_from + 3 WHERE id = :id"),
                         {"id": v1["id"]})
            conn.execute(text("INSERT INTO shift_versions (company_id, shift_id, version_no, effective_from, "
                              "start_time, end_time, crosses_midnight, content_hash) SELECT company_id, shift_id, 9, "
                              "effective_from + 10, start_time, end_time, false, content_hash FROM shift_versions "
                              "WHERE id = :id"), {"id": v1["id"]})
    assert "shift_versions_gap" in str(error.value)


def test_legacy_put_posts_a_version_from_the_owner_tomorrow(admin):
    r = admin.post("/api/v1/shifts", json={"name": "Retail", "start_time": "10:00", "end_time": "19:00"})
    shift_id = r.json()["id"]
    r = admin.put(f"/api/v1/shifts/{shift_id}", json={"name": "Retail", "start_time": "10:00", "end_time": "19:00",
                                                      "unpaid_break_minutes": 60})
    assert r.status_code == 200, r.text
    v1, v2 = _versions(admin, shift_id)
    assert v2["effective_from"] == (_today() + timedelta(days=1)).isoformat()
    assert (v1["unpaid_break_minutes"], v2["unpaid_break_minutes"]) == (None, 60)
    # A rename alone changes the catalogue row, not the versions.
    admin.put(f"/api/v1/shifts/{shift_id}", json={"name": "Retail Day", "start_time": "10:00", "end_time": "19:00",
                                                   "unpaid_break_minutes": 60})
    assert len(_versions(admin, shift_id)) == 2


def test_as_shf_12_archive_is_refused_while_a_placement_uses_the_shift(admin):
    shift = _shift(admin, code="ARCH")
    worker = create_employee(admin, "Ira", shift_id=shift["id"])
    r = admin.post(f"/api/v1/shifts/{shift['id']}/archive", json={})
    assert r.status_code == 409 and r.json()["code"] == "SHIFT_IN_USE"
    assert r.json()["details"]["counts"]["placements"] >= 1
    unused = _shift(admin, code="SPARE")
    r = admin.post(f"/api/v1/shifts/{unused['id']}/archive", json={})
    assert r.status_code == 200 and r.json()["status"] == "ARCHIVED"
    assert all(s["id"] != unused["id"] for s in admin.get("/api/v1/shifts").json())  # BUG-AS-35: active only
    r = admin.post(f"/api/v1/shifts/{unused['id']}/versions",
                   json={"effective_from": (_today() + timedelta(days=1)).isoformat(), "start_time": "08:00",
                         "end_time": "16:00"})
    assert r.status_code == 422 and r.json()["code"] == "SHIFT_ARCHIVED"
    assert admin.post(f"/api/v1/shifts/{unused['id']}/restore", json={}).json()["status"] == "ACTIVE"
    assert worker["id"]


def test_the_kind_and_code_lock_once_a_version_is_in_force(admin):
    shift = _shift(admin, code="LOCK")
    r = admin.patch(f"/api/v1/shifts/{shift['id']}", json={"kind": "FLEXIBLE"})
    assert r.status_code == 409 and r.json()["details"]["reason"] == "KIND_LOCKED"
    r = admin.patch(f"/api/v1/shifts/{shift['id']}", json={"name": "Locked day", "color_token": "shift-4"})
    assert r.status_code == 200 and (r.json()["name"], r.json()["color_token"]) == ("Locked day", "shift-4")
