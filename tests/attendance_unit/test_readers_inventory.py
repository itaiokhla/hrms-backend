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
    # AS-M2 capture v2 (reviewed: every one filters voided_at IS NULL, or reads only its own new session)
    "app/services/attendance_sessions.py::sessions_on": "attendance/test_sessions_constraint.py",
    "app/services/attendance_sessions.py::check_overlap": "attendance/test_sessions_constraint.py",
    "app/services/attendance_jobs.py::close_overdue_sessions": "attendance/test_punch_log.py",
    # MANUAL shadow punches of an HR entry; compares the legacy "absent" status only to skip time-less marks.
    "app/services/attendance_service.py::_manual_punches": "attendance/test_punch_log.py",
    # AS-M2 phase 2: the transparency panel lists Location VIEW audit rows of the viewer's own session ids only (no
    # time or status is read; voided sessions keep their audit history).
    "app/services/attendance_privacy.py::evidence_views": "attendance/test_consents.py",
    # LH-M1 Goal 5: closure impact preview reads who had a session on the closed date (read-only, voided_at IS NULL)
    "app/services/holiday_closures.py::closure_impact": "holiday/test_rollover.py",
    # AS-M3: the evaluation batch loader (non-voided sessions of the range; legacy marks only for the LEGACY_V1 port)
    "app/services/attendance_days.py::load_inputs_many": "attendance/test_attendance_days.py",
    # AS-M4 (G-13): request targets refuse voided sessions; form and muster list non-voided sessions only.
    # Leave v2 comp-off (LH SPINE §8): worked minutes of the claimed date from non-voided closed sessions.
    "app/services/leave_comp_off.py::worked_minutes": "leave/test_v2_core.py",
    "app/services/attendance_requests.py::_session": "attendance/test_requests.py",
    "app/services/attendance_requests.py::_session_for_missing": "attendance/test_requests.py",
    "app/services/attendance_requests.py::form_context": "attendance/test_requests.py",
    "app/services/attendance_muster.py::grid": "attendance/test_requests.py",
    # Team attendance (IG-AS-98..100): the board lists non-voided sessions; delete refuses a voided session.
    "app/services/attendance_team.py::team_today": "attendance/test_team_attendance.py",
    "app/services/attendance_team.py::delete_session": "attendance/test_team_attendance.py",
    # AS-M7 (G-17): device pairing reads and voids only its own non-voided device session (pairing_key).
    "app/services/device_ingest.py::_pair_one": "attendance/test_device_ingest.py",
    # AS-M9 (G-20): the live board counts non-voided sessions of the date (count = list, D-1).
    "app/services/attendance_analytics.py::board": "attendance/test_insights.py",
}


def test_readers_inventory_is_complete():
    found = scan()
    assert sorted(found - set(READERS)) == [], "new attendance reader: review it against spine §15.4 and list it"
    assert sorted(set(READERS) - found) == [], "a listed reader no longer reads sessions: update the inventory"


def test_every_reader_has_a_covering_test_module():
    missing = {reader: module for reader, module in READERS.items() if not (TESTS / module).exists()}
    assert missing == {}
