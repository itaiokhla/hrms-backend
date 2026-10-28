"""Pure geofence, IP, QR and capture-policy rules (04-attendance/01 §9; AS 07 §7; spine §6.2.1, §6.3; WI-AS-2.9).

Edge cases AS-GEO-01, -02, -03, -05, -06, -07, -08 (rules), golden vectors GV-AS-CAP-06 / -09 (evaluation part), the
LEGACY_V1 mapping and v1 clamp of the capture policy, and the "no pack" resolver of AS-CTY-10..12 (SP-AS-223).
"""

import math
import os
from dataclasses import replace

import pytest

for _key, _value in {"DATABASE_URL": "postgresql://x:y@localhost:1/z", "SYSTEM_DATABASE_URL":
                     "postgresql://x:y@localhost:1/z", "SECRET_KEY": "pure-test-secret-key-long-enough-for-hs256",
                     "ENVIRONMENT": "test"}.items():
    os.environ.setdefault(_key, _value)

from app.core.errors import AppError
from app.payroll import policy_types as pt
from app.services import attendance_capture_policy as cp
from app.services import attendance_sites as sites
from app.services import geo, pack_resolver
from app.services.attendance_policy import AttendancePolicy

BLR = (12.9716, 77.5946)
HINJEWADI = (18.5912, 73.7389)
R = 6371008.8


def legacy_settings(**over) -> AttendancePolicy:
    values = dict(require_face=True, require_face_for_lunch=False, allow_multiple_lunches=True, lunch_is_paid=False,
                  lunch_open_at_punch_out="CONFIRM_AUTO_CLOSE", max_shift_hours=16, location_punch="OPTIONAL",
                  location_lunch="OFF", location_exception="ALLOW_WITH_REASON", reject_low_accuracy=False,
                  preferred_accuracy_m=50, acceptable_accuracy_m=100, gps_timeout_seconds=20, gps_max_attempts=3,
                  max_fix_age_seconds=120, tracking_enabled=False, tracking_interval_minutes=120,
                  tracking_grace_minutes=15, pause_on_lunch=True, movement_threshold_m=1000, retention_days=90)
    values.update(over)
    return AttendancePolicy(**values)


def standard(**over) -> cp.CapturePolicy:
    return cp.CapturePolicy(profile="STANDARD", location_mode="REQUIRED", **over)


def at(origin, dx_m: float, dy_m: float):
    """The point dx metres east and dy metres north of ``origin`` (local equirectangular)."""
    lat0, lng0 = origin
    return (lat0 + math.degrees(dy_m / R), lng0 + math.degrees(dx_m / (R * math.cos(math.radians(lat0)))))


SITE_2101 = sites.SiteShape(id=2101, code="BLR-HQ", name="Bengaluru Office", geometry_type="CIRCLE",
                            center_lat=BLR[0], center_lng=BLR[1], radius_m=120, ip_ranges=("203.0.113.0/24",))
# L-shaped yard of 100 m with a 60 x 60 m notch (polygon site 2203, simplified).
L_RING = [(0, 0), (100, 0), (100, 40), (40, 40), (40, 100), (0, 100)]
SITE_2203 = sites.SiteShape(id=2203, code="PUN-HJW", name="Hinjewadi Project Site", geometry_type="POLYGON",
                            polygon=tuple(at(HINJEWADI, x, y) for x, y in L_RING))


def fix(point, accuracy, **extra):
    return sites.LocationFix(latitude=point[0], longitude=point[1], accuracy_m=accuracy, fix_age_seconds=3, **extra)


def east(metres):
    return geo.destination(BLR[0], BLR[1], 90, metres)


# ------------------------------------------------------------------------------------------------- AS-GEO-01
def test_gv_as_cap_06_flag_outside_is_accepted_with_flag_and_review():
    """GV-AS-CAP-06 / AS-GEO-01 (a): 164 m from the centre, accuracy 18 m -> 146 > 120 OUTSIDE; FLAG accepts with flag
    OUTSIDE_GEOFENCE and a WARNING review item; the employee view says "Outside site" with a band, no coordinates."""
    out = sites.evaluate_location([SITE_2101], fix(east(164), 18), standard(geofence_mode="FLAG"))
    assert out.refusal is None and out.result == "OUTSIDE" and out.site_id == 2101
    assert out.flags == ("OUTSIDE_GEOFENCE",) and out.review_kinds == (("OUTSIDE_GEOFENCE", "WARNING"),)
    assert out.distance_band == "WITHIN_250M"
    view = out.employee_view()
    assert view["label"] == "Outside site" and view["site_name"] == "Bengaluru Office"
    assert not {"latitude", "longitude", "accuracy_m", "distance_m"} & set(view)
    blocked = sites.evaluate_location([SITE_2101], fix(east(164), 18), standard(geofence_mode="BLOCK"))
    assert blocked.refusal[0] == "GEOFENCE_BLOCKED" and blocked.refusal[2]["reason_allowed"] is False


def test_as_geo_01_boundary_coarse_fix_and_maximum_tolerance():
    policy = standard(geofence_mode="FLAG")
    assert sites.evaluate_location([SITE_2101], fix(east(138), 18), policy).result == "INSIDE"  # (b) 120 <= 120
    coarse = sites.evaluate_location([SITE_2101], fix(east(30), 101), policy)  # (c) accuracy 101 > 100
    assert coarse.result == "UNKNOWN" and coarse.flags == ("LOW_ACCURACY",)
    assert coarse.review_kinds == (("LOW_ACCURACY", "INFO"),)
    assert sites.evaluate_location([SITE_2101], fix(east(300), 100), policy).result == "OUTSIDE"  # (d) 200 > 120


def test_distance_bands_never_carry_the_distance():
    assert sites.distance_band("OUTSIDE", 44) == "WITHIN_250M"
    assert sites.distance_band("OUTSIDE", 600) == "WITHIN_1KM"
    assert sites.distance_band("OUTSIDE", 1500) == "BEYOND_1KM"
    assert sites.distance_band("INSIDE", 0) == "INSIDE" and sites.distance_band("UNKNOWN", None) == "UNKNOWN"
    assert sites.distance_band("NOT_CHECKED", None) is None


# ------------------------------------------------------------------------------------------------- AS-GEO-02
def test_gv_as_cap_09_polygon_require_reason():
    """GV-AS-CAP-09 / AS-GEO-02 (a): 52 m outside the nearest edge, accuracy 12 -> OUTSIDE -> 422 GEOFENCE_BLOCKED with
    reason_allowed; with a reason (5-500 chars) accepted, flag OUTSIDE_GEOFENCE, review WARNING."""
    policy = standard(geofence_mode="REQUIRE_REASON")
    point = at(HINJEWADI, 50, -52)
    first = sites.evaluate_location([SITE_2203], fix(point, 12), policy)
    assert first.refusal[0] == "GEOFENCE_BLOCKED" and first.refusal[2]["reason_allowed"] is True
    assert "Hinjewadi Project Site" in first.refusal[1]
    short = sites.evaluate_location([SITE_2203], fix(point, 12, exception_reason="yard"), policy)
    assert short.refusal is not None  # under 5 characters
    ok = sites.evaluate_location([SITE_2203], fix(point, 12,
                                 exception_reason="Receiving cement delivery at the yard gate"), policy)
    assert ok.refusal is None and ok.result == "OUTSIDE" and ok.flags == ("OUTSIDE_GEOFENCE",)
    assert ok.review_kinds == (("OUTSIDE_GEOFENCE", "WARNING"),) and ok.site_id == 2203


def test_as_geo_02_polygon_edge_and_concave_notch():
    polygon = SITE_2203.polygon
    assert sites.polygon_distance_m(*at(HINJEWADI, 50, 0), polygon) == 0.0  # (b) on the edge
    assert sites.polygon_distance_m(*at(HINJEWADI, 20, 20), polygon) == 0.0  # inside
    notch = sites.polygon_distance_m(*at(HINJEWADI, 70, 70), polygon)  # (c) 30 m from two edges
    assert abs(notch - 30) < 0.5
    out = sites.evaluate_location([SITE_2203], fix(at(HINJEWADI, 70, 70), 10), standard(geofence_mode="FLAG"))
    assert out.result == "OUTSIDE"  # 30 - 10 = 20 > 0
    assert sites.evaluate_location([SITE_2203], fix(at(HINJEWADI, 50, 0), 5),
                                   standard(geofence_mode="BLOCK")).result == "INSIDE"


# ------------------------------------------------------------------------------------------------- AS-GEO-03
def test_as_geo_03_unknown_location_under_require_reason_and_block():
    shed = fix(at(HINJEWADI, 20, 20), 140)  # inside a steel shed, accuracy 140 m
    rr = sites.evaluate_location([SITE_2203], shed, standard(geofence_mode="REQUIRE_REASON"))
    assert rr.refusal[0] == "GEOFENCE_BLOCKED" and rr.refusal[2]["reason_allowed"] is True
    assert rr.refusal[2]["geofence_result"] == "UNKNOWN" and "can't confirm" in rr.refusal[1]
    allow = sites.evaluate_location([SITE_2203], shed, standard(geofence_mode="BLOCK",
                                                                location_failure_action="ALLOW_WITH_REASON"))
    assert allow.refusal[2]["reason_allowed"] is True  # BLOCK + ALLOW_WITH_REASON behaves as REQUIRE_REASON
    hard = sites.evaluate_location([SITE_2203], shed, standard(geofence_mode="BLOCK", location_failure_action="BLOCK"))
    assert hard.refusal[2]["reason_allowed"] is False
    accepted = sites.evaluate_location([SITE_2203], replace(shed, exception_reason="Inside the steel shed"),
                                       standard(geofence_mode="REQUIRE_REASON"))
    assert accepted.result == "UNKNOWN" and accepted.flags == ("LOW_ACCURACY",)
    assert accepted.review_kinds == (("LOW_ACCURACY", "WARNING"),)


def test_as_geo_05_blocking_geofence_needs_location_required():
    """AS-GEO-05 (V-AS-13): REQUIRE_REASON / BLOCK with location_mode other than REQUIRED is refused at save."""
    with pytest.raises(pt.PolicyValidationError) as error:
        pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", {"geofence_mode": "BLOCK", "location_mode": "OPTIONAL"})
    assert error.value.code == "INVALID_FIELDS" and "location required" in error.value.message
    assert pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", {"geofence_mode": "FLAG"})["geofence_mode"] == "FLAG"


# ------------------------------------------------------------------------------------------------- IP / QR
def test_ip_mode_flag_block_and_g6_match_satisfies_the_site():
    office = sites.LocationFix(client_ip="203.0.113.40")
    home = sites.LocationFix(client_ip="198.51.100.77")
    matched = sites.evaluate_location([SITE_2101], office, standard(ip_mode="BLOCK", geofence_mode="BLOCK"))
    assert matched.refusal is None and matched.site_id == 2101 and matched.result == "NOT_CHECKED"  # G-6
    assert sites.evaluate_location([SITE_2101], home, standard(ip_mode="BLOCK")).refusal[0] == "IP_NOT_ALLOWED"
    flagged = sites.evaluate_location([SITE_2101], home, standard(ip_mode="FLAG"))
    assert flagged.flags == ("IP_NOT_ALLOWED",) and flagged.review_kinds == (("IP_NOT_ALLOWED", "INFO"),)


def test_as_geo_06_private_only_ranges_warn_and_bad_ranges_are_refused():
    ranges, warnings = sites.normalize_ranges(["10.21.0.0/16"])
    assert ranges == ["10.21.0.0/16"] and warnings == ["PRIVATE_RANGES_ONLY"]
    assert sites.normalize_ranges(["203.0.113.0/24", "10.0.0.0/8"])[1] == []
    with pytest.raises(AppError) as error:
        sites.normalize_ranges(["not-a-range"])
    assert error.value.code == "IP_RANGE_INVALID"


def test_as_geo_07_forged_leftmost_forwarded_entry_is_ignored(monkeypatch):
    """AS-GEO-07 / T-28: with trusted proxies the client is the entry trusted_proxy_hops from the right."""
    from starlette.requests import Request

    from app.config import settings
    from app.core.rate_limit import client_ip

    def request(xff):
        return Request({"type": "http", "headers": [(b"x-forwarded-for", xff.encode())], "client": ("10.0.0.9", 1)})

    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert client_ip(request("203.0.113.5, 198.51.100.77")) == "198.51.100.77"
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    assert client_ip(request("203.0.113.5, 198.51.100.77, 10.1.1.1")) == "198.51.100.77"
    forged = sites.evaluate_location([SITE_2101], sites.LocationFix(client_ip="198.51.100.77"), standard(ip_mode="BLOCK"))
    assert forged.refusal[0] == "IP_NOT_ALLOWED"


def test_qr_required_valid_invalid_and_as_geo_08_remote_day():
    secret = "poster-secret-1"
    site = replace(SITE_2101, qr_secret_hash=sites.qr_hash(secret))
    policy = standard(qr_mode="REQUIRED")
    good = sites.evaluate_location([site], sites.LocationFix(qr_payload=sites.qr_payload("BLR-HQ", secret)), policy)
    assert good.refusal is None and good.site_id == 2101
    stale = sites.LocationFix(qr_payload=sites.qr_payload("BLR-HQ", "old-poster"))
    assert sites.evaluate_location([site], stale, policy).refusal[0] == "QR_INVALID"
    assert sites.evaluate_location([site], sites.LocationFix(), policy).refusal[0] == "QR_INVALID"
    # AS-GEO-08: a REMOTE day skips the QR requirement; a stale QR sent anyway is recorded, never required (Q-R3).
    remote = sites.evaluate_location([site], sites.LocationFix(work_mode="REMOTE"), policy)
    assert remote.refusal is None and remote.result == "NOT_CHECKED"
    remote_stale = sites.evaluate_location([site], replace(stale, work_mode="REMOTE"), policy)
    assert remote_stale.refusal is None and remote_stale.flags == ("QR_INVALID",)
    # Q-R3 with qr_mode OFF: an invalid QR is accepted with flag and informational review
    off = sites.evaluate_location([site], stale, standard())
    assert off.flags == ("QR_INVALID",) and off.review_kinds == (("QR_INVALID", "INFO"),)


def test_g7_remote_and_field_days_skip_geofence_and_ip():
    policy = standard(geofence_mode="BLOCK", ip_mode="BLOCK")
    for mode in ("REMOTE", "FIELD"):
        out = sites.evaluate_location([SITE_2101], fix(east(5000), 10, work_mode=mode, client_ip="198.51.100.1"), policy)
        assert out.refusal is None and out.result == "NOT_CHECKED"


# ------------------------------------------------------------------------------------------------- site validation
def test_site_radius_too_small_and_polygon_rules():
    base = {"kind": "OFFICE", "geometry_type": "CIRCLE", "center_lat": BLR[0], "center_lng": BLR[1]}
    with pytest.raises(AppError) as error:
        sites._check_shape({**base, "radius_m": 60})
    assert error.value.code == "SITE_RADIUS_TOO_SMALL"
    sites._check_shape({**base, "radius_m": sites.MIN_RADIUS_M})
    ring = sites.validate_polygon([list(at(HINJEWADI, x, y)) for x, y in L_RING])
    assert ring[0] == ring[-1] and len(ring) == len(L_RING) + 1  # closed
    bow_tie = [list(at(HINJEWADI, x, y)) for x, y in ((0, 0), (100, 100), (100, 0), (0, 100))]
    with pytest.raises(AppError) as error:
        sites.validate_polygon(bow_tie)
    assert error.value.code == "SITE_POLYGON_INVALID"
    with pytest.raises(AppError):
        sites.validate_polygon([list(at(HINJEWADI, 0, 0)), list(at(HINJEWADI, 10, 0))])


# ------------------------------------------------------------------------------------------------- capture policy
def test_legacy_v1_mapping_is_a_valid_version_and_maps_gps_and_face():
    """Spine §6.3 / SP-AS-221: the LEGACY_V1 version of today's settings validates and maps face and GPS."""
    settings = legacy_settings(location_punch="REQUIRED", reject_low_accuracy=True, acceptable_accuracy_m=150)
    fields = pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", cp.legacy_fields(settings))
    assert fields["profile"] == "LEGACY_V1" and fields["face_on_punch"] is True
    assert fields["location_mode"] == "REQUIRED" and fields["gps"]["acceptable_accuracy_m"] == 150
    legacy = cp.legacy_v1(settings)
    assert legacy.allowed_sources == {"WEB", "KIOSK"} and legacy.kiosk_methods == {"FACE"}
    assert legacy.multiple_sessions_allowed is False and legacy.auto_close_mode == "MAX_DURATION"
    assert cp.as_attendance_policy(legacy, settings) is settings  # LEGACY_V1 overlays nothing


def test_standard_version_fields_and_v1_clamp():
    fields = pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", {
        "allowed_sources": ["WEB", "MOBILE", "DEVICE"], "geofence_mode": "FLAG", "location_mode": "OPTIONAL",
        "offline_punches": "ALLOW", "face_on_punch": True, "dpia_reference": "DPIA-ALPHA-2026-07"})
    policy = cp.from_fields(fields, policy_id=7, version=2)
    assert policy.profile == "STANDARD" and policy.multiple_sessions_allowed and policy.offline_punches == "ALLOW"
    clamped = cp.v1_clamp(policy)  # not V2_ACTIVE: one session per date, no offline, interactive sources only
    assert clamped.multiple_sessions_allowed is False and clamped.offline_punches == "OFF"
    assert clamped.allowed_sources == {"WEB", "MOBILE"}
    overlay = cp.as_attendance_policy(policy, legacy_settings(require_face=False))
    assert overlay.require_face is True and overlay.location_punch == "OPTIONAL"


def test_face_needs_a_dpia_reference_on_standard_versions():
    with pytest.raises(pt.PolicyValidationError) as error:
        pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", {"kiosk_methods": ["PIN", "FACE"]})
    assert "dpia_reference" in error.value.message
    assert pt.validate_fields("ATTENDANCE_CAPTURE_POLICY", {"profile": "LEGACY_V1", "face_on_punch": True})


# ------------------------------------------------------------------------------------------------- packs (SP-AS-223)
@pytest.mark.parametrize("field", sorted(pack_resolver.NO_PACK_DEFAULTS))
def test_as_cty_10_no_pack_path(field):
    """AS-CTY-10 on the "no pack" path (SP-AS-223): every field answers NO_PACK — no biometric gate on 12 or 13 May
    2027 (the product consent rule applies on both dates), punch photos allowed, no retention cap."""
    from datetime import date

    value = pack_resolver.resolve(None, 1, 11, field, date(2027, 5, 13))
    assert value.source == pack_resolver.NO_PACK and value.value == pack_resolver.NO_PACK_DEFAULTS[field]
    assert pack_resolver.biometrics_prohibited(None, 1, 11, date(2027, 5, 12)) is False
    assert pack_resolver.punch_photo_allowed(None, 1, 11, date(2027, 5, 12)) is True


def test_as_cty_11_resolver_answers_no_pack_for_india_after_dpdp():
    """AS-CTY-11: on 2027-06-01 the resolver returns the "no pack" value (no enum is invented before G-14); the
    non-biometric alternative stays a product rule: PIN is a kiosk method of every default policy."""
    from datetime import date

    value = pack_resolver.resolve(None, 1, 11, "privacy.biometric_timekeeping", date(2027, 6, 1))
    assert (value.value, value.source, value.verified) == (None, "NO_PACK", False)
    assert "PIN" in cp.CapturePolicy().kiosk_methods


def test_as_cty_12_prohibited_pack_value_stops_face_at_once(monkeypatch):
    """AS-CTY-12: no pack -> FACE is not stopped; a PROHIBITED value (as G-14 will deliver for FR) makes face_allowed
    false before any consent or legacy check, so the alternative applies at once."""
    from datetime import date

    from app.services import attendance_consents

    assert not pack_resolver.biometrics_prohibited(None, 1, 31, date(2027, 3, 1))
    monkeypatch.setattr(attendance_consents, "_legal_entity_on", lambda db, cid, eid, on: 31)
    monkeypatch.setattr(pack_resolver, "resolve", lambda db, cid, le, field, on: pack_resolver.PackValue(
        field, "PROHIBITED" if le == 31 else None, "PACK"))
    assert attendance_consents.face_allowed(None, 1, 7, date(2027, 3, 1), profile="LEGACY_V1") is False
    assert attendance_consents.can_enrol(None, 1, 7, date(2027, 3, 1)) is None
