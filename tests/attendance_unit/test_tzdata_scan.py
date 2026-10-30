"""EC AS-TZ-03 (02-system-design §7.4 runbook TZDATA-1): comparing two tz database rule sets for one zone.

The DB half (the publication, change rows, notifications, idempotence) is
tests/shifts/test_roster_edge_cases.py::test_as_tz_03_*.
"""

from datetime import datetime, timedelta, timezone

from app.services import tzdata_scan as tz

UTC = timezone.utc
START = datetime(2026, 10, 30, tzinfo=UTC)
END = START + timedelta(days=tz.WINDOW_DAYS)


def test_as_tz_03_a_zone_that_stops_falling_back_differs_from_the_change_date_only():
    """The EC's scenario: from Sun 2027-11-07 New York stays on EDT. The installed rules fall back to EST at 06:00 UTC
    that day; the new release does not. The first difference is that instant; before it both agree; the offsets of
    published instants decide which entries move."""
    installed = tz.installed_rules("America/New_York", START, END)
    assert installed["initial"] == -240  # EDT on 30 Oct 2026
    fall_2027 = datetime(2027, 11, 7, 6, 0, tzinfo=UTC)
    assert [t for t, m in installed["transitions"]][:3] == [
        "2026-11-01T06:00:00+00:00", "2027-03-14T07:00:00+00:00", fall_2027.isoformat()]
    proposed = {"initial": installed["initial"],
                "transitions": [[t, m] for t, m in installed["transitions"] if datetime.fromisoformat(t) < fall_2027]}
    assert tz.first_difference(installed, proposed, START, END) == fall_2027
    assert tz.offset_at(installed, fall_2027 + timedelta(hours=12)) == -300
    assert tz.offset_at(proposed, fall_2027 + timedelta(hours=12)) == -240
    assert tz.offset_at(installed, datetime(2027, 6, 1, tzinfo=UTC)) == tz.offset_at(
        proposed, datetime(2027, 6, 1, tzinfo=UTC)) == -240
    assert tz.first_difference(installed, installed, START, END) is None


def test_as_tz_03_a_zone_without_transitions_never_differs_from_itself():
    kolkata = tz.installed_rules("Asia/Kolkata", START, END)
    assert kolkata == {"initial": 330, "transitions": []}
    assert tz.first_difference(kolkata, kolkata, START, END) is None
    assert tz.first_difference(kolkata, {"initial": 300, "transitions": []}, START, END) == START
