"""Canonical hours calculator and geodesic movement classification (pure functions)."""

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from app.services import geo
from app.services.attendance_hours import BreakInterval, compute, decimal_hours, total

IST = pytz.timezone("Asia/Kolkata")
NY = pytz.timezone("America/New_York")


def ist(day: str, hhmm: str, second: int = 0, micro: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    y, mo, d = (int(x) for x in day.split("-"))
    return IST.localize(datetime(y, mo, d, h, m, second, micro))


DAY = "2026-10-19"


def test_goal_example_one_lunch():
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), [BreakInterval(ist(DAY, "13:00"), ist(DAY, "13:45"))])
    assert (hours.gross_seconds, hours.lunch_seconds, hours.excluded_seconds, hours.net_seconds) == (9 * 3600, 45 * 60, 45 * 60, 8 * 3600 + 15 * 60)
    assert hours.complete and not hours.provisional and hours.issues == ()


def test_goal_example_two_lunches():
    breaks = [BreakInterval(ist(DAY, "12:30"), ist(DAY, "13:00")), BreakInterval(ist(DAY, "15:30"), ist(DAY, "15:45"))]
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), breaks)
    assert hours.net_seconds == 8 * 3600 + 15 * 60 and hours.lunch_seconds == 45 * 60


def test_no_lunch_net_equals_gross():
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"))
    assert hours.gross_seconds == hours.net_seconds == 9 * 3600 and hours.lunch_seconds == 0


def test_paid_lunch_is_reported_but_not_deducted():
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), [BreakInterval(ist(DAY, "13:00"), ist(DAY, "13:45"), paid=True)])
    assert hours.lunch_seconds == 2700 and hours.excluded_seconds == 0 and hours.net_seconds == 9 * 3600


def test_ongoing_lunch_grows_break_while_net_stays_flat():
    start = ist(DAY, "09:00")
    lunch = BreakInterval(ist(DAY, "13:00"), None)
    at_1310 = compute(start, None, [lunch], now=ist(DAY, "13:10"))
    at_1340 = compute(start, None, [lunch], now=ist(DAY, "13:40"))
    assert at_1310.provisional and at_1340.provisional
    assert at_1340.lunch_seconds - at_1310.lunch_seconds == 30 * 60
    assert at_1340.gross_seconds - at_1310.gross_seconds == 30 * 60
    assert at_1310.net_seconds == at_1340.net_seconds == 4 * 3600


def test_overnight_shift_across_midnight_and_month_end():
    start = ist("2026-10-31", "22:00")
    end = ist("2026-11-01", "06:30")
    hours = compute(start, end, [BreakInterval(ist("2026-11-01", "02:00"), ist("2026-11-01", "02:30"))])
    assert hours.gross_seconds == 8 * 3600 + 30 * 60 and hours.net_seconds == 8 * 3600


def test_dst_spring_forward_counts_real_elapsed_time():
    # 2026-03-08 02:00 EST jumps to 03:00 EDT: 22:00 -> 06:00 local is 7 real hours, not 8.
    start = NY.localize(datetime(2026, 3, 7, 22, 0))
    end = NY.localize(datetime(2026, 3, 8, 6, 0))
    assert compute(start, end).gross_seconds == 7 * 3600


def test_dst_fall_back_counts_real_elapsed_time():
    start = NY.localize(datetime(2026, 10, 31, 22, 0))
    end = NY.localize(datetime(2026, 11, 1, 6, 0))
    assert compute(start, end).gross_seconds == 9 * 3600


def test_seconds_are_truncated_per_instant_so_parts_add_up():
    hours = compute(ist(DAY, "09:00", 0, 900_000), ist(DAY, "18:00", 0, 100_000),
                    [BreakInterval(ist(DAY, "13:00", 59, 999_999), ist(DAY, "13:45", 0, 1))])
    assert hours.gross_seconds == 32400  # not 32399 from fractional subtraction
    assert hours.lunch_seconds == 45 * 60 - 59
    assert hours.net_seconds == hours.gross_seconds - hours.excluded_seconds


def test_overlapping_breaks_are_never_deducted_twice():
    breaks = [BreakInterval(ist(DAY, "13:00"), ist(DAY, "13:45")), BreakInterval(ist(DAY, "13:30"), ist(DAY, "14:00"))]
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), breaks)
    assert hours.excluded_seconds == 60 * 60 and "OVERLAPPING_BREAKS" in hours.issues


def test_breaks_outside_session_are_ignored_or_clipped():
    breaks = [BreakInterval(ist(DAY, "08:00"), ist(DAY, "08:30")), BreakInterval(ist(DAY, "17:30"), ist(DAY, "18:30")),
              BreakInterval(ist(DAY, "12:00"), ist(DAY, "11:00"))]
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), breaks)
    assert hours.excluded_seconds == 30 * 60
    assert {"BREAK_OUTSIDE_SESSION", "BREAK_CLIPPED_TO_SESSION", "NEGATIVE_BREAK"} <= set(hours.issues)
    assert 0 <= hours.excluded_seconds <= hours.gross_seconds


def test_back_to_back_breaks_are_not_an_overlap():
    breaks = [BreakInterval(ist(DAY, "13:00"), ist(DAY, "13:30")), BreakInterval(ist(DAY, "13:30"), ist(DAY, "13:45"))]
    hours = compute(ist(DAY, "09:00"), ist(DAY, "18:00"), breaks)
    assert hours.excluded_seconds == 45 * 60 and hours.issues == ()


def test_missing_punch_out_is_incomplete_not_invented():
    hours = compute(ist(DAY, "09:00"), None, [BreakInterval(ist(DAY, "13:00"), ist(DAY, "13:45"))], live=False)
    assert hours.gross_seconds is None and hours.net_seconds is None and not hours.complete
    assert hours.lunch_seconds == 2700 and "MISSING_PUNCH_OUT" in hours.issues


def test_running_session_requires_now():
    with pytest.raises(ValueError):
        compute(ist(DAY, "09:00"), None)
    with pytest.raises(ValueError):
        compute(datetime(2026, 10, 19, 9), datetime(2026, 10, 19, 18))  # naive datetimes are refused


def test_totals_skip_incomplete_sessions_and_convert_after_summing():
    sessions = [
        compute(ist(DAY, "09:00"), ist(DAY, "09:00", 20)),  # 20 s
        compute(ist(DAY, "10:00"), ist(DAY, "10:00", 20)),  # 20 s
        compute(ist(DAY, "11:00"), None, live=False),
    ]
    summary = total(sessions)
    assert summary["net_seconds"] == 40 and summary["incomplete_sessions"] == 1 and summary["complete_sessions"] == 2
    assert decimal_hours(summary["net_seconds"]) == 0.01  # 40 s, converted once
    assert decimal_hours(29700) == 8.25


# Geodesic classification ------------------------------------------------------
BLR = (12.9716, 77.5946)


def test_geodesic_distance_matches_known_value():
    lat, lon = geo.destination(*BLR, 0, 1000)
    assert geo.distance_m(*BLR, lat, lon) == pytest.approx(1000, abs=0.01)
    # One degree of latitude at the equator on WGS-84 is 110,574.4 m.
    assert geo.distance_m(0, 0, 1, 0) == pytest.approx(110574.39, abs=0.05)


@pytest.mark.parametrize("lat,lon", [(90, 0), (-90, 0), (0, 180), (0, -180), (89.9999, 179.9999)])
def test_distance_handles_coordinate_boundaries(lat, lon):
    assert geo.distance_m(lat, lon, lat, lon) == 0


def test_movement_classification_respects_uncertainty():
    threshold = 1000
    # Exactly at the threshold after subtracting both radii: not proven movement.
    assert geo.classify_movement(1040.0, 20, 20, threshold, 100) == geo.UNCERTAIN
    assert geo.classify_movement(1040.01, 20, 20, threshold, 100) == geo.SIGNIFICANT_MOVEMENT
    assert geo.classify_movement(960.0, 20, 20, threshold, 100) == geo.WITHIN_RANGE
    assert geo.classify_movement(30.0, 25, 25, threshold, 100) == geo.WITHIN_RANGE  # jitter inside uncertainty
    assert geo.classify_movement(5000.0, 2500, 20, threshold, 100) == geo.LOW_ACCURACY  # Wi-Fi/IP-grade fix
    assert geo.classify_movement(None, 20, None, threshold, 100) == geo.BASELINE


def test_exact_threshold_from_constructed_points():
    lat, lon = geo.destination(*BLR, 90, 1040)
    distance = geo.distance_m(*BLR, lat, lon)
    assert distance == 1040.0
    assert geo.classify_movement(distance, 20, 20, 1000, 100) == geo.UNCERTAIN


def test_accuracy_classes():
    assert geo.accuracy_class(50, 50, 100) == geo.PREFERRED
    assert geo.accuracy_class(100, 50, 100) == geo.ACCEPTABLE
    assert geo.accuracy_class(100.5, 50, 100) == geo.LOW
