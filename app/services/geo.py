"""Geodesic distances and movement classification for attendance location samples.

Distances are straight-line (geodesic) displacements on the WGS-84 ellipsoid
(Karney's algorithm via geographiclib), not a travelled route. Browser
accuracy values are 95% confidence radii, so two readings are only treated as
"moved" when the displacement exceeds the threshold even after subtracting both
radii, and as "within range" only when it stays inside the threshold after
adding them. Anything in between is UNCERTAIN, never verified movement.
Classifications trigger human review; they never discipline or change pay.
"""

from typing import Optional

from geographiclib.geodesic import Geodesic

_WGS84 = Geodesic.WGS84

BASELINE = "BASELINE"
WITHIN_RANGE = "WITHIN_RANGE"
UNCERTAIN = "UNCERTAIN"
LOW_ACCURACY = "LOW_ACCURACY"
SIGNIFICANT_MOVEMENT = "SIGNIFICANT_MOVEMENT_REVIEW"

PREFERRED, ACCEPTABLE, LOW = "PREFERRED", "ACCEPTABLE", "LOW"


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Geodesic distance in metres, rounded to the centimetre."""
    return round(_WGS84.Inverse(lat1, lon1, lat2, lon2, Geodesic.DISTANCE)["s12"], 2)


def destination(lat: float, lon: float, bearing_deg: float, metres: float):
    """Point reached from (lat, lon) after `metres` along `bearing_deg` (used by tests and fixtures)."""
    result = _WGS84.Direct(lat, lon, bearing_deg, metres)
    return result["lat2"], result["lon2"]


def accuracy_class(accuracy_m: float, preferred_m: float, acceptable_m: float) -> str:
    if accuracy_m <= preferred_m:
        return PREFERRED
    if accuracy_m <= acceptable_m:
        return ACCEPTABLE
    return LOW


def classify_movement(
    distance: Optional[float],
    accuracy_m: float,
    reference_accuracy_m: Optional[float],
    threshold_m: float,
    acceptable_accuracy_m: float,
) -> str:
    """Classify displacement from the reference reading (normally the punch-in fix)."""
    if distance is None or reference_accuracy_m is None:
        return BASELINE
    if accuracy_m > acceptable_accuracy_m or reference_accuracy_m > acceptable_accuracy_m:
        return LOW_ACCURACY
    uncertainty = accuracy_m + reference_accuracy_m
    if distance - uncertainty > threshold_m:
        return SIGNIFICANT_MOVEMENT
    if distance + uncertainty <= threshold_m:
        return WITHIN_RANGE
    return UNCERTAIN
