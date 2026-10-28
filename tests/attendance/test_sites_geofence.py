"""Sites, geofence, IP and QR end to end (AS-M2 WI-AS-2.9; 04-attendance/01 §9, §4.2 steps 14-19; GV-AS-CAP-06, -09;
EC AS-GEO-01..03, -06..-08; AS 07 §18 T-03, T-28).

The pure rules are in tests/attendance_unit/test_geofence.py; here the routes, the punch pipeline (punch row, sample,
review item), the site admin (SITE_RADIUS_TOO_SMALL, private-range warning, network test, archive, QR rotation) and
tenant isolation.
"""

import pytest

from app.services import geo
from tests.attendance.capture_v2_helpers import (
    activate_capture, create_site, location, punches, review_items, rows, v2_punch,
)
from tests.attendance_helpers import employee_with_login, fake_clock, set_policy  # noqa: F401
from tests.helpers import signup_company

BLR = (12.9716, 77.5946)
HJW = (18.5912, 73.7389)


@pytest.fixture
def alpha(app):
    admin = signup_company(app, "Alpha Sites Co")  # Asia/Kolkata
    set_policy(admin, requireFaceForSelfCheckIn=False)
    priya = employee_with_login(app, admin, "Priya")
    return {"admin": admin, "priya": priya, "priya_id": priya.me()["employee"]["id"]}


def _east(metres, origin=BLR):
    return geo.destination(origin[0], origin[1], 90, metres)


def test_gv_as_cap_06_priya_outside_the_office_with_flag(alpha, fake_clock):
    """GV-AS-CAP-06 / AS-GEO-01 (a): Priya MOBILE 164 m from the site 2101 centre (radius 120, accuracy 18), geofence
    FLAG -> accepted (USED), flag OUTSIDE_GEOFENCE, one review item per employee and date, the sample carries the
    punch and its result, and Priya sees "Outside site" with a band — never coordinates."""
    site = create_site(alpha["admin"])
    activate_capture(alpha["admin"], {"allowed_sources": ["WEB", "MOBILE"], "location_mode": "OPTIONAL",
                                      "geofence_mode": "FLAG", "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-03", "10:12", 40)
    lat, lng = _east(164)
    r = v2_punch(alpha["priya"], fake_clock, capture_source="MOBILE", location=location(fake_clock, lat, lng, 18))
    assert r.status_code == 200, r.text
    check = r.json()["location_check"]
    assert check == {"result": "OUTSIDE", "label": "Outside site", "distance_band": "WITHIN_250M",
                     "site_name": "Bengaluru Office", "flags": ["OUTSIDE_GEOFENCE"]}
    punch = punches(alpha["priya_id"])[0]
    assert (punch["disposition"], punch["geofence_result"], punch["site_id"]) == ("USED", "OUTSIDE", site["id"])
    assert punch["evidence_flags"] == ["OUTSIDE_GEOFENCE"]
    items = review_items(alpha["priya_id"])
    assert [(i["kind"], i["severity"], i["dedup_key"]) for i in items] == [
        ("OUTSIDE_GEOFENCE", "WARNING", f"OUTSIDE_GEOFENCE:{alpha['priya_id']}:2026-11-03")]
    sample = rows("SELECT punch_id, site_id, geofence_result, distance_to_site_m FROM location_samples "
                  "WHERE employee_id = :e", e=alpha["priya_id"])[0]
    assert (sample["punch_id"], sample["site_id"], sample["geofence_result"]) == (punch["id"], site["id"], "OUTSIDE")
    assert 40 <= float(sample["distance_to_site_m"]) <= 48  # 164 - 120, to the boundary
    # The reviewer (Kavya / the admin) sees the item in the queue.
    queue = alpha["admin"].get("/api/v1/attendance/review").json()
    assert [i["kind"] for i in queue["items"]] == ["OUTSIDE_GEOFENCE"]


def test_gv_as_cap_06_with_block_refuses_and_writes_nothing(alpha, fake_clock):
    site = create_site(alpha["admin"])
    activate_capture(alpha["admin"], {"allowed_sources": ["WEB", "MOBILE"], "location_mode": "REQUIRED",
                                      "geofence_mode": "BLOCK", "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-03", "10:12", 40)
    lat, lng = _east(164)
    r = v2_punch(alpha["priya"], fake_clock, capture_source="MOBILE", location=location(fake_clock, lat, lng, 18))
    assert r.status_code == 422 and r.json()["code"] == "GEOFENCE_BLOCKED", r.text
    details = r.json()["details"]
    assert details["reason_allowed"] is False and details["distance_band"] == "WITHIN_250M"
    assert "latitude" not in r.text and str(round(lat, 4)) not in r.text
    assert punches(alpha["priya_id"]) == [] and review_items() == []
    inside = v2_punch(alpha["priya"], fake_clock, capture_source="MOBILE",
                      location=location(fake_clock, *_east(60), 15))
    assert inside.status_code == 200 and inside.json()["location_check"]["result"] == "INSIDE"


def test_gv_as_cap_09_prakash_polygon_require_reason(alpha, fake_clock):
    """GV-AS-CAP-09 / AS-GEO-02 (a): 52 m outside the polygon site 2203, accuracy 12 -> 422 GEOFENCE_BLOCKED with
    reason_allowed; with a reason the punch is accepted with flag and a WARNING review item."""
    from tests.attendance_unit.test_geofence import L_RING, at

    ring = [list(at(HJW, x, y)) for x, y in L_RING]
    site = create_site(alpha["admin"], code="PUN-HJW", name="Hinjewadi Project Site", kind="PROJECT_SITE",
                       geometry_type="POLYGON", polygon=ring, center_lat=None, center_lng=None, radius_m=None)
    assert site["polygon"][0] == site["polygon"][-1]
    activate_capture(alpha["admin"], {"location_mode": "REQUIRED", "geofence_mode": "REQUIRE_REASON",
                                      "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-13", "08:03", 40)
    yard = at(HJW, 50, -52)
    first = v2_punch(alpha["priya"], fake_clock, location=location(fake_clock, yard[0], yard[1], 12))
    assert first.status_code == 422 and first.json()["code"] == "GEOFENCE_BLOCKED", first.text
    assert first.json()["details"]["reason_allowed"] is True
    assert punches(alpha["priya_id"]) == []
    again = v2_punch(alpha["priya"], fake_clock, location=location(fake_clock, yard[0], yard[1], 12),
                     exception_reason="Receiving cement delivery at the yard gate")
    assert again.status_code == 200, again.text
    punch = punches(alpha["priya_id"])[0]
    assert punch["evidence_flags"] == ["OUTSIDE_GEOFENCE"] and punch["geofence_result"] == "OUTSIDE"
    assert [(i["kind"], i["severity"]) for i in review_items(alpha["priya_id"])] == [("OUTSIDE_GEOFENCE", "WARNING")]


def test_as_geo_03_steel_shed_unknown_needs_a_reason(alpha, fake_clock):
    site = create_site(alpha["admin"], radius_m=200)
    activate_capture(alpha["admin"], {"location_mode": "REQUIRED", "geofence_mode": "REQUIRE_REASON",
                                      "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-13", "17:05", 12)
    r = v2_punch(alpha["priya"], fake_clock, location=location(fake_clock, *_east(30), 140))
    assert r.status_code == 422 and r.json()["details"]["geofence_result"] == "UNKNOWN"
    ok = v2_punch(alpha["priya"], fake_clock, location=location(fake_clock, *_east(30), 140),
                  exception_reason="Inside the steel shed")
    assert ok.status_code == 200 and ok.json()["location_check"]["result"] == "UNKNOWN"
    assert punches(alpha["priya_id"])[0]["evidence_flags"] == ["LOW_ACCURACY"]


def test_as_geo_03_reject_low_accuracy_answers_before_the_site_check(alpha, fake_clock):
    site = create_site(alpha["admin"])
    activate_capture(alpha["admin"], {"location_mode": "REQUIRED", "reject_low_accuracy": True,
                                      "location_failure_action": "BLOCK", "geofence_mode": "REQUIRE_REASON",
                                      "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-13", "17:05")
    r = v2_punch(alpha["priya"], fake_clock, location=location(fake_clock, *_east(30), 140))
    assert r.status_code == 422 and r.json()["code"] == "LOCATION_LOW_ACCURACY"


def test_ip_block_and_t_28_forged_forwarded_entry(alpha, fake_clock, monkeypatch):
    """§9.4 / AS-GEO-07 / T-28: ip_mode BLOCK matches the office range through the trusted-hop entry; a forged leftmost
    X-Forwarded-For entry never satisfies it; a match sets the site (G-6) without a geofence."""
    from app.config import settings

    site = create_site(alpha["admin"], ip_ranges=["203.0.113.0/24"])
    activate_capture(alpha["admin"], {"ip_mode": "BLOCK", "allowed_site_ids": [site["id"]]})
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    fake_clock.at_ist("2026-11-23", "09:00")
    forged = v2_punch(alpha["priya"], fake_clock, headers={"X-Forwarded-For": "203.0.113.5, 198.51.100.77"})
    assert forged.status_code == 422 and forged.json()["code"] == "IP_NOT_ALLOWED"
    office = v2_punch(alpha["priya"], fake_clock, headers={"X-Forwarded-For": "198.51.100.77, 203.0.113.5"})
    assert office.status_code == 200, office.text
    punch = punches(alpha["priya_id"])[0]
    assert (punch["site_id"], punch["geofence_result"], punch["evidence_flags"]) == (site["id"], "NOT_CHECKED", [])


def test_ip_flag_accepts_with_an_informational_item(alpha, fake_clock):
    site = create_site(alpha["admin"], ip_ranges=["203.0.113.0/24"])
    activate_capture(alpha["admin"], {"ip_mode": "FLAG", "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-23", "09:00")
    assert v2_punch(alpha["priya"], fake_clock).status_code == 200  # TestClient address "testclient"
    assert punches(alpha["priya_id"])[0]["evidence_flags"] == ["IP_NOT_ALLOWED"]
    assert [(i["kind"], i["severity"]) for i in review_items()] == [("IP_NOT_ALLOWED", "INFO")]


def test_qr_required_rotation_and_as_geo_08_remote_day(alpha, fake_clock):
    """Z-6 / Q-R2 / Q-R3 / AS-GEO-08: the poster payload is shown once; the current poster passes, a rotated one is
    QR_INVALID; a REMOTE answer needs no QR."""
    admin = alpha["admin"]
    site = create_site(admin)
    activate_capture(admin, {"qr_mode": "REQUIRED", "allowed_site_ids": [site["id"]]})
    first = admin.post(f"/api/v1/attendance/sites/{site['id']}/rotate-qr").json()["qr_payload"]
    second = admin.post(f"/api/v1/attendance/sites/{site['id']}/rotate-qr").json()["qr_payload"]
    assert "qr_payload" not in admin.get(f"/api/v1/attendance/sites/{site['id']}").json()
    fake_clock.at_ist("2026-11-02", "09:58")
    missing = v2_punch(alpha["priya"], fake_clock)
    assert missing.status_code == 422 and missing.json()["code"] == "QR_INVALID"
    stale = v2_punch(alpha["priya"], fake_clock, qr_payload=first)
    assert stale.status_code == 422 and stale.json()["code"] == "QR_INVALID"
    assert punches(alpha["priya_id"]) == []
    remote = v2_punch(alpha["priya"], fake_clock, work_location_choice="REMOTE")
    assert remote.status_code == 200, remote.text
    fake_clock.at_ist("2026-11-02", "18:30")
    out = v2_punch(alpha["priya"], fake_clock, "OUT", qr_payload=second)
    assert out.status_code == 200 and punches(alpha["priya_id"])[-1]["site_id"] == site["id"]


def test_site_admin_rules(alpha, app):
    """Z-2 SITE_RADIUS_TOO_SMALL; Z-5 private ranges saved with a warning (AS-GEO-06); network test; archive; the
    employee has no site access; another tenant cannot read the site (T-03)."""
    admin = alpha["admin"]
    small = create_site(admin, status=422, radius_m=60)
    assert small["code"] == "SITE_RADIUS_TOO_SMALL" and small["details"]["min_radius_m"] == 100
    site = create_site(admin, ip_ranges=["10.21.0.0/16"])
    assert site["warnings"] == ["PRIVATE_RANGES_ONLY"] and site["ip_ranges"] == ["10.21.0.0/16"]
    assert create_site(admin, status=409)["code"] == "CONFLICT"
    test = admin.get("/api/v1/attendance/sites/network-test", params={"site_id": site["id"]}).json()
    assert test["client_ip"] == "testclient" and test["matches"] == []
    stale = admin.patch(f"/api/v1/attendance/sites/{site['id']}", json={"radius_m": 150}, headers={"If-Match": "9"})
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_VERSION"
    changed = admin.patch(f"/api/v1/attendance/sites/{site['id']}", json={"radius_m": 150},
                          headers={"If-Match": str(site["row_version"])})
    assert changed.status_code == 200 and changed.json()["radius_m"] == 150 and changed.json()["row_version"] == 2
    archived = admin.post(f"/api/v1/attendance/sites/{site['id']}/archive", json={})
    assert archived.status_code == 200 and archived.json()["status"] == "ARCHIVED"
    again = admin.post(f"/api/v1/attendance/sites/{site['id']}/archive", json={})
    assert again.status_code == 409 and again.json()["code"] == "INVALID_TRANSITION"
    assert alpha["priya"].get("/api/v1/attendance/sites").status_code == 403
    other = signup_company(app, "Beta Sites Co")
    assert other.get(f"/api/v1/attendance/sites/{site['id']}").status_code == 404
    assert other.get("/api/v1/attendance/sites").json()["total"] == 0
    audit = rows("SELECT action FROM audit_logs WHERE action = 'SITE_SAVED' AND company_id = :c",
                 c=admin.company_id)
    assert len(audit) == 3


def test_my_capture_shows_site_names_without_geometry(alpha, fake_clock):
    site = create_site(alpha["admin"])
    activate_capture(alpha["admin"], {"location_mode": "OPTIONAL", "geofence_mode": "FLAG",
                                      "allowed_site_ids": [site["id"]]})
    fake_clock.at_ist("2026-11-03", "09:00")
    mine = alpha["priya"].get("/api/v1/me/attendance/capture")
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert body["profile"] == "STANDARD" and body["geofence_mode"] == "FLAG"
    assert body["sites"] == [{"id": site["id"], "name": "Bengaluru Office", "kind": "OFFICE"}]
    assert "center_lat" not in mine.text and "radius" not in mine.text
