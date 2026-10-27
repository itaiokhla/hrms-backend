"""Readers inventory of attendance sessions (WI-AS-0.3; spine §15.4, AS 11 §11.6; merge gate LH-G-6).

Every function under app/ that queries the Attendance model or reads the legacy status vocabulary is listed here
with the test module that covers it. A new reader fails the first test until it is reviewed and listed (with its
v2 change, spine §15.4); a reader whose covering test module disappears fails the second. Readers not in spine
§15.4 at G-01 are marked "not in spine" (FOLLOW-UPS FU-AS-03).
"""

from pathlib import Path

from tests.attendance_unit.readers_scan import scan

TESTS = Path(__file__).resolve().parents[1]

# reader -> covering test module (relative to tests/)
READERS = {
    # spine §15.4 rows
    "app/payroll/services/time_inputs.py::_attendance_inputs": "attendance/test_m0_goldens.py",
    "app/payroll/services/snapshot.py::_Builder.shifts_json": "attendance/test_m0_goldens.py",
    "app/payroll/services/overtime.py::_worked": "attendance/test_m0_goldens.py",
    "app/services/attendance_service.py::today_summary": "leave/test_m0_goldens.py",
    "app/services/attendance_service.py::trend": "leave/test_m0_goldens.py",
    "app/services/attendance_service.py::monthly_stats": "leave/test_m0_goldens.py",
    "app/services/reports_service.py::attendance_report": "test_attendance_isolation.py",
    "app/services/payroll_service.py::calculate": "payroll/test_legacy_golden.py",
    # attendance service internals (the punch pipeline itself; spine §15.4 cites content() only)
    "app/services/attendance_service.py::_record_for_day": "test_attendance_lifecycle.py",
    "app/services/attendance_service.py::apply_timing": "attendance/test_m0_goldens.py",
    "app/services/attendance_service.py::hours_summary": "test_attendance_lifecycle.py",
    "app/services/attendance_service.py::list_records": "test_attendance_lifecycle.py",
    "app/services/attendance_service.py::live_session": "test_attendance_lifecycle.py",
    "app/services/attendance_service.py::month_hours": "test_attendance_isolation.py",
    "app/services/attendance_service.py::my_today": "test_attendance_lifecycle.py",
    "app/services/attendance_service.py::recent_activity": "test_workflows.py",
    "app/services/attendance_service.py::record_manual": "attendance/test_m0_goldens.py",
    # G-01 BUG-AS-04 / -05 / -14: an "absent" entry may come without times (reviewed: input handling only).
    "app/services/attendance_service.py::_manual_times": "attendance/test_m0_bug_fixes.py",
    "app/services/attendance_service.py::session_detail": "test_attendance_location.py",
    # not in spine §15.4 (FU-AS-03)
    "app/cli.py::_legacy_recompute": "payroll/test_legacy_inventory.py",
    "app/services/location_service.py::apply_policy_change": "test_attendance_location.py",
    "app/services/location_service.py::review_queue": "test_attendance_review_fixes.py",
    "app/services/location_service.py::sweep": "test_attendance_location.py",
    "app/services/reports_service.py::dashboard_statistics": "test_workflows.py",
    "app/services/reports_service.py::department_analytics": "test_cross_tenant_attacks.py",
    "app/services/reports_service.py::employee_analytics": "test_cross_tenant_attacks.py",
}


def test_readers_inventory_is_complete():
    found = scan()
    assert sorted(found - set(READERS)) == [], "new attendance reader: review it against spine §15.4 and list it"
    assert sorted(set(READERS) - found) == [], "a listed reader no longer reads sessions: update the inventory"


def test_every_reader_has_a_covering_test_module():
    missing = {reader: module for reader, module in READERS.items() if not (TESTS / module).exists()}
    assert missing == {}
