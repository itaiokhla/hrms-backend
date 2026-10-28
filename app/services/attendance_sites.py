"""Sites, geofence, IP allow-lists and QR at punch time (04-attendance/01 §9; spine §3.5; D-AS-27).

Sites (``attendance_sites``, Z-1..Z-8): CIRCLE (centre + radius >= ``attendance.geofence.min_radius_m`` -> 422
``SITE_RADIUS_TOO_SMALL``), POLYGON (closed ring of [lat, lng], <= 64 points, simple, edges <= 5 km) or NONE (IP / QR
only); ``ip_ranges`` (CIDR, private ranges saved with a warning, AS-GEO-06); a QR secret stored only as its SHA-256
(``rotate_qr`` shows the poster payload once). Sites are archived, never deleted.

Punch check (``evaluate_punch`` -> ``evaluate_location``, the pure helper; rules G-1..G-8, §9.4, Q-R1..Q-R3):

* the sites that apply (§9.2): the policy's ``allowed_site_ids``, else the ACTIVE sites of the employee's branch on the
  shift date; REMOTE / Home / Field days skip site, IP and QR checks (G-7, P-6) — a QR sent anyway is still checked;
* G-1 a fix is usable when accuracy <= ``gps.acceptable_accuracy_m`` and fresh; else ``UNKNOWN`` (+ ``LOW_ACCURACY``
  when a coarse fix exists); G-2 geodesic distance to a circle centre, shortest distance to a polygon edge (0 inside,
  local equirectangular projection); G-3 INSIDE when ``distance - accuracy <= radius`` (circle) or ``<= 0`` (polygon);
  G-4 nearest INSIDE site, else the nearest site; G-6 an office-network match satisfies the site check;
* ``geofence_mode`` FLAG (accept + flag + review), REQUIRE_REASON (422 ``GEOFENCE_BLOCKED`` with
  ``details.reason_allowed = true`` until ``meta.exception_reason`` is given), BLOCK (422 ``GEOFENCE_BLOCKED``;
  UNKNOWN with ``location_failure_action = ALLOW_WITH_REASON`` behaves as REQUIRE_REASON); ``ip_mode`` FLAG / BLOCK
  (422 ``IP_NOT_ALLOWED``); ``qr_mode`` REQUIRED (422 ``QR_INVALID``); G-8 a result never decides status or pay.

Employee-facing answers carry the site name, the result and a distance band (07 §7: INSIDE, WITHIN_250M,
WITHIN_1KM, BEYOND_1KM, UNKNOWN) — never coordinates or the measured distance. Services never commit.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.errors import AppError, conflict, forbidden, not_found
from app.models.attendance import SITE_GEOMETRY_TYPES, SITE_KINDS, AttendanceSite
from app.services import geo
from app.services.attendance_capture_policy import CapturePolicy
from app.services.audit_log_service import AuditLogService

MANAGE = "attendance:manage_sites"
MIN_RADIUS_M = 100  # attendance.geofence.min_radius_m (registry key in AS-M3; FOLLOW-UPS)
MAX_POLYGON_POINTS = 64
MAX_EDGE_M = 5000
REASON_MIN, REASON_MAX = 5, 500
QR_PREFIX = "HRMSQR1"
BAND_LABELS = {"INSIDE": "At {site}", "WITHIN_250M": "Near {site} (within 250 m)",
               "WITHIN_1KM": "Outside {site} (within 1 km)", "BEYOND_1KM": "Far from {site} (over 1 km)",
               "UNKNOWN": "Location not confirmed"}
RESULT_LABELS = {"INSIDE": "At site", "OUTSIDE": "Outside site", "UNKNOWN": "Location not confirmed"}


@dataclass(frozen=True)
class LocationFix:
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    fix_age_seconds: Optional[int] = None
    client_ip: Optional[str] = None
    qr_payload: Optional[str] = None
    work_mode: str = "ON_SITE"  # P-6: a missing Office / Home / Field answer is ON_SITE
    exception_reason: Optional[str] = None


@dataclass(frozen=True)
class GeofenceOutcome:
    result: str = "NOT_CHECKED"  # GEOFENCE_RESULTS
    site_id: Optional[int] = None
    distance_m: Optional[float] = None  # to the nearest site boundary (0 inside); stored on the sample only
    flags: Tuple[str, ...] = ()  # EVIDENCE_FLAGS to store on the punch
    review_kinds: Tuple[Tuple[str, str], ...] = ()  # (REVIEW_KINDS value, severity) to open
    site_name: Optional[str] = None
    distance_band: Optional[str] = None  # DISTANCE_BANDS; None = no chip (NOT_CHECKED)
    refusal: Optional[Tuple[str, str, Mapping[str, Any]]] = field(default=None, compare=False)  # (code, msg, details)

    def employee_view(self) -> Optional[dict]:
        """What the punching employee may see (07 §7, §8 E1): result, band and site name — no coordinates."""
        if self.result == "NOT_CHECKED" and self.site_id is None:
            return None
        return {"result": self.result, "label": RESULT_LABELS.get(self.result), "distance_band": self.distance_band,
                "site_name": self.site_name, "flags": list(self.flags)}


NOT_CHECKED = GeofenceOutcome()


@dataclass(frozen=True)
class SiteShape:
    """The pure view of one ``attendance_sites`` row used by ``evaluate_location``."""

    id: int
    code: str
    name: str
    geometry_type: str = "NONE"
    center_lat: Optional[float] = None
    center_lng: Optional[float] = None
    radius_m: Optional[float] = None
    polygon: Tuple[Tuple[float, float], ...] = ()
    ip_ranges: Tuple[str, ...] = ()
    qr_secret_hash: Optional[str] = None

    @classmethod
    def of(cls, row: AttendanceSite) -> "SiteShape":
        return cls(id=row.id, code=row.code, name=row.name, geometry_type=row.geometry_type,
                   center_lat=float(row.center_lat) if row.center_lat is not None else None,
                   center_lng=float(row.center_lng) if row.center_lng is not None else None,
                   radius_m=float(row.radius_m) if row.radius_m is not None else None,
                   polygon=tuple((float(p[0]), float(p[1])) for p in (row.polygon or [])),
                   ip_ranges=tuple(str(r) for r in (row.ip_ranges or [])), qr_secret_hash=row.qr_secret_hash)


# ------------------------------------------------------------------------------------------------- geometry (pure)
def _project(lat: float, lng: float, lat0: float, lng0: float) -> Tuple[float, float]:
    """Local equirectangular projection in metres around (lat0, lng0) (error negligible at site scale)."""
    radius = 6371008.8
    x = math.radians(lng - lng0) * radius * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * radius
    return x, y


def _segment_distance(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ring(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    ring = [tuple(p) for p in points]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def polygon_distance_m(lat: float, lng: float, polygon: Sequence[Tuple[float, float]]) -> float:
    """0 when the point is inside the ring (or on an edge), else the shortest distance to an edge (G-2)."""
    ring = _ring(polygon)
    lat0 = sum(p[0] for p in ring) / len(ring)
    lng0 = sum(p[1] for p in ring) / len(ring)
    pts = [_project(p[0], p[1], lat0, lng0) for p in ring]
    q = _project(lat, lng, lat0, lng0)
    edges = list(zip(pts, pts[1:] + pts[:1]))
    nearest = min(_segment_distance(q, a, b) for a, b in edges)
    if nearest < 1e-6:
        return 0.0
    inside = False
    x, y = q
    for (ax, ay), (bx, by) in edges:
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return 0.0 if inside else round(nearest, 1)


def _boundary_distance(site: SiteShape, lat: float, lng: float) -> Optional[float]:
    """Distance from the fix to the site's area boundary (0 inside) and the comparison base of G-3."""
    if site.geometry_type == "CIRCLE" and site.center_lat is not None and site.center_lng is not None:
        return geo.distance_m(site.center_lat, site.center_lng, lat, lng) - float(site.radius_m or 0)
    if site.geometry_type == "POLYGON" and len(_ring(site.polygon)) >= 3:
        return polygon_distance_m(lat, lng, site.polygon)
    return None


def distance_band(result: str, boundary_m: Optional[float]) -> Optional[str]:
    if result == "INSIDE":
        return "INSIDE"
    if result == "UNKNOWN":
        return "UNKNOWN"
    if result != "OUTSIDE" or boundary_m is None:
        return None
    if boundary_m <= 250:
        return "WITHIN_250M"
    return "WITHIN_1KM" if boundary_m <= 1000 else "BEYOND_1KM"


def ip_in_ranges(client_ip: Optional[str], ranges: Iterable[str]) -> bool:
    try:
        address = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return False
    for value in ranges:
        try:
            if address in ipaddress.ip_network(value, strict=False):
                return True
        except ValueError:
            continue
    return False


def qr_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def qr_payload(site_code: str, secret: str) -> str:
    return f"{QR_PREFIX}:{site_code}:{secret}"


def qr_site(sites: Sequence[SiteShape], payload: Optional[str]) -> Optional[SiteShape]:
    """The allowed site whose current poster secret the payload carries (constant-time compare), else None."""
    parts = (payload or "").strip().split(":")
    if len(parts) != 3 or parts[0] != QR_PREFIX:
        return None
    code, secret = parts[1], parts[2]
    for site in sites:
        if site.code == code and site.qr_secret_hash and hmac.compare_digest(site.qr_secret_hash, qr_hash(secret)):
            return site
    return None


def _reason_ok(reason: Optional[str]) -> bool:
    return reason is not None and REASON_MIN <= len(reason.strip()) <= REASON_MAX


# ------------------------------------------------------------------------------------------------- evaluation (pure)
def evaluate_location(site_rows: Sequence[SiteShape], fix: LocationFix, policy: CapturePolicy) -> GeofenceOutcome:
    """Pure G-1..G-8, §9.4 and Q-R1..Q-R3 over the applicable sites. A refusal is returned in ``refusal``."""
    flags: List[str] = []
    reviews: List[Tuple[str, str]] = []
    skipped = fix.work_mode in ("REMOTE", "FIELD")  # G-7 / P-6
    qr_checked = qr_site(site_rows, fix.qr_payload) if fix.qr_payload else None
    site_id = qr_checked.id if qr_checked else None
    site_name = qr_checked.name if qr_checked else None
    if fix.qr_payload and qr_checked is None:
        if policy.qr_mode == "REQUIRED" and not skipped:
            return GeofenceOutcome(refusal=("QR_INVALID", "This QR code isn't valid for your workplace. Ask your "
                                                          "admin for the current poster.", {}))
        flags.append("QR_INVALID")  # Q-R3: accepted, informational
        reviews.append(("QR_INVALID", "INFO"))
    if policy.qr_mode == "REQUIRED" and not skipped and not fix.qr_payload:
        return GeofenceOutcome(refusal=("QR_INVALID", "Scan the QR poster at your workplace to punch.", {}))
    if skipped:
        return GeofenceOutcome(site_id=site_id, site_name=site_name, flags=tuple(flags), review_kinds=tuple(reviews))
    # §9.4 office network; a match satisfies the site check (G-6).
    if policy.ip_mode != "OFF":
        matched = next((s for s in site_rows if s.ip_ranges and ip_in_ranges(fix.client_ip, s.ip_ranges)), None)
        if matched is not None:
            return GeofenceOutcome(site_id=matched.id, site_name=matched.name, flags=tuple(flags),
                                   review_kinds=tuple(reviews))
        if policy.ip_mode == "BLOCK":
            return GeofenceOutcome(refusal=("IP_NOT_ALLOWED", "Punching from this network isn't allowed. Connect to "
                                                              "the office network.", {}))
        flags.append("IP_NOT_ALLOWED")
        reviews.append(("IP_NOT_ALLOWED", "INFO"))
    if policy.geofence_mode == "OFF":
        return GeofenceOutcome(site_id=site_id, site_name=site_name, flags=tuple(flags), review_kinds=tuple(reviews))
    shapes = [s for s in site_rows if s.geometry_type in ("CIRCLE", "POLYGON")]
    if not shapes:  # V-AS-07 warns in the editor; a punch is never refused for a missing site
        return GeofenceOutcome(site_id=site_id, site_name=site_name, flags=tuple(flags), review_kinds=tuple(reviews))
    has_fix = fix.latitude is not None and fix.longitude is not None
    coarse = has_fix and (fix.accuracy_m is None or fix.accuracy_m > policy.acceptable_accuracy_m)
    stale = has_fix and fix.fix_age_seconds is not None and fix.fix_age_seconds > policy.max_fix_age_seconds
    boundary = None
    nearest = None
    if has_fix and not coarse and not stale:
        accuracy = float(fix.accuracy_m or 0)
        measured = [(s, _boundary_distance(s, fix.latitude, fix.longitude)) for s in shapes]
        measured = [(s, d) for s, d in measured if d is not None]
        inside = sorted(((s, d) for s, d in measured if d - accuracy <= 0), key=lambda x: x[1])
        if inside:
            nearest, boundary, result = inside[0][0], max(inside[0][1], 0.0), "INSIDE"
        elif measured:
            nearest, boundary = min(measured, key=lambda x: x[1])
            result = "OUTSIDE"
        else:
            result = "UNKNOWN"
    else:
        result = "UNKNOWN"
        nearest = shapes[0] if len(shapes) == 1 else None
    site = nearest
    band = distance_band(result, boundary)
    name = site.name if site is not None else (site_name or None)
    outcome_site = site.id if site is not None else site_id
    distance = round(max(boundary, 0.0), 1) if boundary is not None else None
    if result == "INSIDE":
        return GeofenceOutcome(result=result, site_id=outcome_site, distance_m=distance, flags=tuple(flags),
                               review_kinds=tuple(reviews), site_name=name, distance_band=band)
    mode = policy.geofence_mode
    if mode == "BLOCK" and result == "UNKNOWN" and policy.location_failure_action == "ALLOW_WITH_REASON":
        mode = "REQUIRE_REASON"
    where = name or "your workplace"
    details = {"geofence_result": result, "distance_band": band, "site_name": name,
               "label": RESULT_LABELS.get(result)}
    if mode == "BLOCK":
        message = (f"You're outside {where} ({BAND_LABELS[band].format(site=where).lower()}). Move closer and try "
                   f"again." if result == "OUTSIDE" else "We can't confirm your location. Move to open sky and try again.")
        return GeofenceOutcome(refusal=("GEOFENCE_BLOCKED", message, {**details, "reason_allowed": False}))
    if mode == "REQUIRE_REASON" and not _reason_ok(fix.exception_reason):
        message = (f"You're outside {where}. Add a reason to punch from here." if result == "OUTSIDE"
                   else "We can't confirm your location. Add a reason to punch anyway.")
        return GeofenceOutcome(refusal=("GEOFENCE_BLOCKED", message, {**details, "reason_allowed": True}))
    severity = "WARNING" if mode == "REQUIRE_REASON" else None
    if result == "OUTSIDE":
        flags.append("OUTSIDE_GEOFENCE")
        reviews.append(("OUTSIDE_GEOFENCE", severity or "WARNING"))
    elif coarse:
        flags.append("LOW_ACCURACY")
        reviews.append(("LOW_ACCURACY", severity or "INFO"))
    elif severity:  # REQUIRE_REASON accepted with a reason but no fix at all
        reviews.append(("LOW_ACCURACY", severity))
    return GeofenceOutcome(result=result, site_id=outcome_site, distance_m=distance, flags=tuple(flags),
                           review_kinds=tuple(reviews), site_name=name, distance_band=band)


# ------------------------------------------------------------------------------------------------- punch-time loading
def _branch_on(db: Session, company_id: int, employee_id: int, d: date) -> Optional[int]:
    from app.models.org import EmployeeAssignment

    row = (db.query(EmployeeAssignment.branch_id)
           .filter(EmployeeAssignment.company_id == company_id, EmployeeAssignment.employee_id == employee_id,
                   EmployeeAssignment.is_primary.is_(True), EmployeeAssignment.effective_from <= d,
                   or_(EmployeeAssignment.effective_to.is_(None), EmployeeAssignment.effective_to >= d))
           .order_by(EmployeeAssignment.effective_from.desc()).first())
    return row[0] if row else None


def _active_on(query, d: date):
    return query.filter(AttendanceSite.status == "ACTIVE", AttendanceSite.effective_from <= d,
                        or_(AttendanceSite.effective_to.is_(None), AttendanceSite.effective_to >= d))


def sites_for(db: Session, company_id: int, employee_id: int, policy: CapturePolicy, d: date) -> List[AttendanceSite]:
    """§9.2: the policy's allowed sites, else the ACTIVE sites of the employee's branch on ``d``."""
    query = _active_on(db.query(AttendanceSite).filter(AttendanceSite.company_id == company_id), d)
    if policy.allowed_site_ids:
        query = query.filter(AttendanceSite.id.in_(list(policy.allowed_site_ids)))
    else:
        branch_id = _branch_on(db, company_id, employee_id, d)
        query = query.filter(AttendanceSite.branch_id == branch_id) if branch_id is not None \
            else query.filter(AttendanceSite.branch_id.is_(None))
    return query.order_by(AttendanceSite.id).all()


def _needs_check(fix: LocationFix, policy: CapturePolicy) -> bool:
    return bool(fix.qr_payload) or policy.geofence_mode != "OFF" or policy.ip_mode != "OFF" \
        or policy.qr_mode != "OFF"


def evaluate_punch(db: Session, company_id: int, employee_id: int, fix: LocationFix, policy: CapturePolicy,
                   d: date) -> GeofenceOutcome:
    """Site / IP / QR checks of an interactive punch on shift date ``d``; raises 422 when the policy blocks."""
    if not _needs_check(fix, policy):
        return NOT_CHECKED
    rows = sites_for(db, company_id, employee_id, policy, d)
    outcome = evaluate_location([SiteShape.of(r) for r in rows], fix, policy)
    if outcome.refusal is not None:
        code, message, details = outcome.refusal
        raise AppError(422, message, code, extra={"details": dict(details)})
    return outcome


# ------------------------------------------------------------------------------------------------- admin (CRUD)
SITE_FIELDS = ("name", "kind", "legal_entity_id", "branch_id", "timezone", "geometry_type", "center_lat",
               "center_lng", "radius_m", "polygon", "ip_ranges", "outdoor", "effective_from", "effective_to")


def _invalid(message: str, code: str = "VALIDATION_ERROR", **details) -> AppError:
    return AppError(422, message, code, extra={"details": details} if details else None)


def normalize_ranges(values: Optional[Iterable[str]]) -> Tuple[List[str], List[str]]:
    """Canonical CIDR strings and the warnings of Z-5 / AS-GEO-06 (private-only ranges)."""
    ranges: List[str] = []
    warnings: List[str] = []
    for raw in values or ():
        try:
            network = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            raise _invalid(f"{raw!r} is not an IP address or range.", "IP_RANGE_INVALID", value=str(raw))
        if str(network) not in ranges:
            ranges.append(str(network))
    if ranges and all(is_private_range(r) for r in ranges):
        warnings.append("PRIVATE_RANGES_ONLY")
    return ranges, warnings


# RFC 1918, loopback, link-local, ULA (AS-GEO-06): addresses the SaaS never sees as a client.
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
                                                   "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10",
                                                   "::1/128"))


def is_private_range(value: str) -> bool:
    network = ipaddress.ip_network(value, strict=False)
    return any(network.version == p.version and network.subnet_of(p) for p in _PRIVATE)


def validate_polygon(points: Any) -> List[List[float]]:
    if not isinstance(points, (list, tuple)):
        raise _invalid("A polygon is a list of [latitude, longitude] points.", "SITE_POLYGON_INVALID")
    ring: List[Tuple[float, float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise _invalid("Each polygon point is [latitude, longitude].", "SITE_POLYGON_INVALID")
        lat, lng = float(p[0]), float(p[1])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise _invalid("A polygon point is outside the valid range.", "SITE_POLYGON_INVALID")
        ring.append((lat, lng))
    ring = _ring(ring)
    if len(set(ring)) < 3 or len(ring) > MAX_POLYGON_POINTS:
        raise _invalid(f"A polygon needs 3 to {MAX_POLYGON_POINTS} distinct points.", "SITE_POLYGON_INVALID")
    edges = list(zip(ring, ring[1:] + ring[:1]))
    if any(geo.distance_m(a[0], a[1], b[0], b[1]) > MAX_EDGE_M for a, b in edges):
        raise _invalid("A polygon edge is longer than 5 km.", "SITE_POLYGON_INVALID")
    lat0 = sum(p[0] for p in ring) / len(ring)
    lng0 = sum(p[1] for p in ring) / len(ring)
    flat = [_project(a[0], a[1], lat0, lng0) for a in ring]
    segs = list(zip(flat, flat[1:] + flat[:1]))

    def crosses(s1, s2) -> bool:
        def orient(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        (a, b), (c, d) = s1, s2
        return (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0)

    n = len(segs)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if crosses(segs[i], segs[j]):
                raise _invalid("The polygon crosses itself.", "SITE_POLYGON_INVALID")
    return [[lat, lng] for lat, lng in ring] + [[ring[0][0], ring[0][1]]]


def _check_shape(values: Mapping[str, Any]) -> None:
    geometry = values.get("geometry_type")
    if geometry not in SITE_GEOMETRY_TYPES:
        raise _invalid("Choose a circle, a polygon or no area.")
    if values.get("kind") not in SITE_KINDS:
        raise _invalid("Unknown site kind.")
    if geometry == "CIRCLE":
        if values.get("center_lat") is None or values.get("center_lng") is None or values.get("radius_m") is None:
            raise _invalid("A circle needs a centre and a radius.")
        if int(values["radius_m"]) < MIN_RADIUS_M:
            raise AppError(422, f"Use a radius of at least {MIN_RADIUS_M} m: phones place people only to about "
                                f"{MIN_RADIUS_M} m.", "SITE_RADIUS_TOO_SMALL",
                           extra={"details": {"min_radius_m": MIN_RADIUS_M}})
    if geometry == "POLYGON" and not values.get("polygon"):
        raise _invalid("A polygon site needs its points.", "SITE_POLYGON_INVALID")
    if values.get("effective_to") and values.get("effective_from") and values["effective_to"] < values["effective_from"]:
        raise _invalid("The end date is before the start date.", "INVALID_RANGE")


def _require_company(principal) -> None:
    if not principal.has_company_scope(MANAGE):
        raise forbidden("Managing sites needs company-wide site access.")


def serialize_site(row: AttendanceSite, warnings: Sequence[str] = ()) -> dict:
    return {"id": row.id, "code": row.code, "name": row.name, "kind": row.kind,
            "legal_entity_id": row.legal_entity_id, "branch_id": row.branch_id, "timezone": row.timezone,
            "geometry_type": row.geometry_type,
            "center_lat": float(row.center_lat) if row.center_lat is not None else None,
            "center_lng": float(row.center_lng) if row.center_lng is not None else None,
            "radius_m": row.radius_m, "polygon": row.polygon, "ip_ranges": [str(r) for r in (row.ip_ranges or [])],
            "qr_configured": row.qr_secret_hash is not None, "outdoor": row.outdoor, "status": row.status,
            "effective_from": row.effective_from.isoformat(),
            "effective_to": row.effective_to.isoformat() if row.effective_to else None,
            "row_version": row.row_version, "warnings": list(warnings)}


def _apply(db: Session, row: AttendanceSite, values: Mapping[str, Any]) -> List[str]:
    from app.models.org import Branch, LegalEntity

    merged = {k: getattr(row, k) for k in SITE_FIELDS} | dict(values)
    if merged.get("geometry_type") == "POLYGON" and "polygon" in values:
        merged["polygon"] = validate_polygon(values["polygon"])
    _check_shape(merged)
    for key, model in (("branch_id", Branch), ("legal_entity_id", LegalEntity)):
        if merged.get(key) is not None and db.query(model.id).filter(model.company_id == row.company_id,
                                                                     model.id == merged[key]).first() is None:
            raise not_found("Branch" if key == "branch_id" else "Legal entity")
    if merged.get("branch_id") is not None and merged.get("legal_entity_id") is None:
        merged["legal_entity_id"] = db.query(Branch.legal_entity_id).filter(Branch.id == merged["branch_id"]).scalar()
    ranges, warnings = normalize_ranges(merged.get("ip_ranges"))
    merged["ip_ranges"] = ranges or None
    if merged["geometry_type"] != "CIRCLE":
        merged["center_lat"] = merged["center_lat"] if merged["geometry_type"] == "POLYGON" else None
        merged["center_lng"] = merged["center_lng"] if merged["geometry_type"] == "POLYGON" else None
        merged["radius_m"] = None
    if merged["geometry_type"] != "POLYGON":
        merged["polygon"] = None
    for key in ("center_lat", "center_lng"):
        if merged.get(key) is not None:
            merged[key] = Decimal(str(round(float(merged[key]), 6)))
    for key in SITE_FIELDS:
        setattr(row, key, merged.get(key))
    return warnings


def create_site(db: Session, principal, data: Mapping[str, Any], today: date) -> dict:
    _require_company(principal)
    cid = principal.company_id
    code = (data.get("code") or "").strip()
    if db.query(AttendanceSite.id).filter(AttendanceSite.company_id == cid, AttendanceSite.code == code).first():
        raise conflict(f"Site code {code} is taken.", "CONFLICT")
    row = AttendanceSite(company_id=cid, code=code, created_by_user_id=principal.user_id, status="ACTIVE",
                         row_version=1, outdoor=False)
    values = {k: data.get(k) for k in SITE_FIELDS}
    values["effective_from"] = values["effective_from"] or today
    values["outdoor"] = bool(values["outdoor"])
    warnings = _apply(db, row, values)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "SITE_SAVED", f"Site {row.code} created", "attendance_site",
                        row.id, details={"code": row.code, "geometry_type": row.geometry_type})
    return serialize_site(row, warnings)


def load_site(db: Session, company_id: int, site_id: int, *, lock: bool = False) -> AttendanceSite:
    query = db.query(AttendanceSite).filter(AttendanceSite.company_id == company_id, AttendanceSite.id == site_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise not_found("Site")
    return row


def _check_version(row: AttendanceSite, expected: Optional[int]) -> None:
    if expected is not None and expected != row.row_version:
        raise conflict("This site changed meanwhile. Reload and try again.", "STALE_VERSION")


def update_site(db: Session, principal, site_id: int, data: Mapping[str, Any], expected: Optional[int]) -> dict:
    _require_company(principal)
    row = load_site(db, principal.company_id, site_id, lock=True)
    _check_version(row, expected)
    if row.status != "ACTIVE":
        raise conflict("An archived site is not edited.", "INVALID_TRANSITION")
    warnings = _apply(db, row, {k: v for k, v in data.items() if k in SITE_FIELDS})
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "SITE_SAVED", f"Site {row.code} changed", "attendance_site",
                        row.id, details={"changed": sorted(k for k in data if k in SITE_FIELDS)})
    return serialize_site(row, warnings)


def archive_site(db: Session, principal, site_id: int, expected: Optional[int], reason: Optional[str]) -> dict:
    _require_company(principal)
    row = load_site(db, principal.company_id, site_id, lock=True)
    _check_version(row, expected)
    if row.status != "ACTIVE":
        raise conflict("This site is already archived.", "INVALID_TRANSITION")
    row.status = "ARCHIVED"
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "SITE_SAVED", f"Site {row.code} archived", "attendance_site",
                        row.id, details={"status": "ARCHIVED"})
    return serialize_site(row)


def rotate_qr(db: Session, principal, site_id: int, expected: Optional[int]) -> dict:
    """Z-6: a new poster secret (shown once in ``qr_payload``); the old poster stops working at once."""
    _require_company(principal)
    row = load_site(db, principal.company_id, site_id, lock=True)
    _check_version(row, expected)
    if row.status != "ACTIVE":
        raise conflict("An archived site has no poster.", "INVALID_TRANSITION")
    secret = secrets.token_urlsafe(24)
    row.qr_secret_hash = qr_hash(secret)
    row.row_version += 1
    db.flush()
    AuditLogService.log(db, principal, "Attendance", "SITE_SAVED", f"Site {row.code} QR poster rotated",
                        "attendance_site", row.id, details={"qr_rotated": True})
    return {**serialize_site(row), "qr_payload": qr_payload(row.code, secret)}


def list_sites(db: Session, principal, *, status: Optional[str] = None, branch_id: Optional[int] = None,
               q: Optional[str] = None, skip: int = 0, limit: int = 25) -> dict:
    if not principal.has(MANAGE):
        raise forbidden()
    query = db.query(AttendanceSite).filter(AttendanceSite.company_id == principal.company_id)
    if status:
        query = query.filter(AttendanceSite.status == status)
    if branch_id is not None:
        query = query.filter(AttendanceSite.branch_id == branch_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(AttendanceSite.code.ilike(like), AttendanceSite.name.ilike(like)))
    total = query.count()
    rows = query.order_by(AttendanceSite.code, AttendanceSite.id).offset(skip).limit(limit).all()
    return {"items": [serialize_site(r) for r in rows], "total": total, "skip": skip, "limit": limit}


def network_test(db: Session, principal, client_ip: str, *, site_id: Optional[int] = None,
                 branch_id: Optional[int] = None) -> dict:
    """``GET /attendance/sites/network-test`` (SP-AS-239, AS-GEO-06): the caller's address as the server sees it
    after the trusted-hop rule and the ranges that match; read-only."""
    if not principal.has(MANAGE):
        raise forbidden()
    try:
        address = ipaddress.ip_address(client_ip)
        version, private = address.version, is_private_range(str(address))
    except ValueError:
        version, private = None, None
    query = db.query(AttendanceSite).filter(AttendanceSite.company_id == principal.company_id,
                                            AttendanceSite.status == "ACTIVE")
    if site_id is not None:
        query = query.filter(AttendanceSite.id == site_id)
    elif branch_id is not None:
        query = query.filter(AttendanceSite.branch_id == branch_id)
    matches = []
    for row in query.order_by(AttendanceSite.id):
        hits = [str(r) for r in (row.ip_ranges or []) if ip_in_ranges(client_ip, [str(r)])]
        if hits:
            matches.append({"site_id": row.id, "site_code": row.code, "ranges": hits})
    hint = "Add your office's IPv6 prefix too: phones often reach us over IPv6." if version == 4 else None
    return {"client_ip": client_ip, "ip_version": version, "is_private": private, "matches": matches,
            "ipv6_hint": hint}


def names(db: Session, company_id: int, site_ids: Iterable[int]) -> dict:
    ids = sorted({i for i in site_ids if i})
    if not ids:
        return {}
    return {i: n for i, n in db.query(AttendanceSite.id, AttendanceSite.name).filter(
        AttendanceSite.company_id == company_id, AttendanceSite.id.in_(ids))}
