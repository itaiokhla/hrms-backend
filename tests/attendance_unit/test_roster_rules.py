"""Pure publish validation (WI-AS-5.2 / 5.6): GV-AS-ROS-* (pure parts) and the IN parts of GV-AS-CTY-01..03.

Owner doc ``docs/attendance-shift/03-shifts/02-rosters-rotations-assignment.md`` (§6 rules, §14 worked examples,
§15 vectors); IN values appendix-a §8 (DRAFT pack: statutory rows at most WARN, night work REQUIRE_REASON).
Instances come from ``shift_engine.build_instance`` exactly as the publish service will build them. No database.
"""

import datetime as dt
from decimal import Decimal
from functools import lru_cache

import pytz

from app.services import roster_rules as rr
from app.services import rotation_engine as rot
from app.services import shift_engine as se

D = dt.date
IST, NY = "Asia/Kolkata", "America/New_York"
V = se.ShiftVersionData
B = se.BreakRuleData


def at(zone, y, mo, d, h, mi=0):
    """Local wall time -> aware UTC instant."""
    return se.resolve_local_time(zone, dt.datetime(y, mo, d, h, mi))


def floating(start, end, minutes, paid, next_day=False):
    return B("LUNCH", "FLOATING_WINDOW", minutes, paid, window_start=start, window_end=end, next_day=next_day)


# Alpha Pune shifts (paid 30-min floating lunch, 480 net each) and the Gamma NYC store shifts
SHIFT_A, SHIFT_B, SHIFT_C = 201, 202, 203
A = V(start_time="06:00", end_time="14:00", break_rules=(floating("09:30", "11:30", 30, True),), shift_id=SHIFT_A)
B_SHIFT = V(start_time="14:00", end_time="22:00", break_rules=(floating("17:30", "19:30", 30, True),), shift_id=SHIFT_B)
C = V(start_time="22:00", end_time="06:00", break_rules=(floating("01:30", "03:30", 30, True, True),), shift_id=SHIFT_C)
VERSIONS = {SHIFT_A: A, SHIFT_B: B_SHIFT, SHIFT_C: C}
NY_OPEN = V(start_time="08:30", end_time="17:00", break_rules=(floating("12:00", "14:00", 30, False),), shift_id=411)
NY_CLOSE = V(start_time="13:30", end_time="22:00", break_rules=(floating("17:00", "19:00", 30, False),), shift_id=412)
GEN = V(start_time="09:00", end_time="18:00", shift_id=101,
        break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="13:00", end="14:00"),))

PUNE_PATTERN = rot.TEMPLATES["SIX_ON_TWO_OFF_SLOW"].pattern({"M": SHIFT_A, "A": SHIFT_B, "N": SHIFT_C})
PUNE_SHIFTS = {SHIFT_A: rot.ShiftTimes("06:00", "14:00", 480), SHIFT_B: rot.ShiftTimes("14:00", "22:00", 480),
               SHIFT_C: rot.ShiftTimes("22:00", "06:00", 480)}
PUNE_ANCHOR = D(2026, 11, 2)
CREWS = ("A", "B", "C", "D")
SUNITA, RAVI = 3001, 2001  # CREW-C (woman, protected group WOMEN), CREW-B
CREW_MEMBERS = {c: tuple(range(1000 * (i + 2) + 1, 1000 * (i + 2) + 11)) for i, c in enumerate(CREWS)}
# B: 2001..2010 (Ravi 2001), C: 3001..3010 (Sunita 3001)


@lru_cache(maxsize=None)
def instance(shift_id, day, zone=IST):
    return se.build_instance(VERSIONS[shift_id], day, zone)


def work(version, day, zone, **flags):
    return rr.from_shift_instance(se.build_instance(version, day, zone), **flags)


def custom(day, start, end, zone, breaks=(), **flags):
    """A custom-time entry; ``breaks`` are the resolved ATTENDANCE_RULES_POLICY.break_defaults."""
    entry = se.RosterEntryData(custom_start_time=start, custom_end_time=end, break_defaults=breaks)
    return rr.from_shift_instance(se.build_instance(None, day, zone, entry), **flags)


def off(day, **flags):
    return rr.PlannedInstance(day, rr.OFF, **flags)


def pune_employee(employee_id, crew_code, start=D(2026, 11, 1), end=D(2026, 11, 30), changed=True, edit=None):
    assignment = rot.assignment_for_crew(PUNE_PATTERN, crew_code, PUNE_ANCHOR)
    items = []
    for p in rot.expand(PUNE_PATTERN, assignment, start, end):
        if edit and p.date in edit:
            items.append(edit[p.date](p))
        elif p.kind == "WORK":
            items.append(rr.from_shift_instance(instance(p.shift, p.date), changed=changed))
        else:
            items.append(off(p.date, changed=changed))
    groups = {"WOMEN"} if employee_id == SUNITA else set()
    return rr.EmployeeSchedule(employee_id, IST, tuple(items), employment_start=D(2020, 1, 1),
                               protected_groups=frozenset(groups))


def pune_roster(changed=True, edits=None):
    edits = edits or {}
    return rr.ScheduleData(tuple(pune_employee(e, c, changed=changed, edit=edits.get(c))
                                 for c in CREWS for e in CREW_MEMBERS[c]))


def in_draft_limits(**overrides):
    """IN pack DRAFT (appendix-a §8.2, owner doc §6.3): statutory rows WARN, night work REQUIRE_REASON."""
    values = dict(
        weekly_max_hours=rr.Limit(48, rr.WARN, "WT-IN hours.weekly_max_including_ot_hours", verified=False),
        max_days_per_week=rr.Limit(6, rr.WARN, "LH weekly_rest IN max_days_per_week", verified=False),
        max_consecutive_days=rr.Limit(10, rr.WARN, "LH weekly_rest IN max_consecutive_days", verified=False),
        weekly_rest_min_hours=rr.Limit(24, rr.WARN, "WT-IN rest.weekly_min_hours", verified=False),
        week_definition=rr.ROLLING,
        night_work_groups={"WOMEN": rr.NightWorkRule(("19:00", "06:00"), True, rr.REQUIRE_REASON,
                                                     "WT-IN night_work.protected_groups[WOMEN]", verified=False)},
    )
    values.update(overrides)
    return rr.EmployeeLimits(**values)


PUNE_COVERAGE = tuple(rr.CoverageRule(s, 9, rule_ref=f"IN_FACTORY_24X7 coverage {s}") for s in (SHIFT_A, SHIFT_B, SHIFT_C))
SUNITA_CONSENT = rr.Consent(SUNITA, rr.NIGHT_WORK, valid_from=D(2026, 1, 1), consent_id=91)


def keys(results, severity=None):
    return {r.rule_key for r in results if severity is None or r.severity == severity}


def of(results, key):
    return [r for r in results if r.rule_key == key]


# --------------------------------------------------------------------------- plain cases


def test_min_rest_8h_between_two_8h_shifts_blocks():
    morning = custom(D(2026, 11, 3), "06:00", "14:00", IST, changed=True)
    night = custom(D(2026, 11, 3), "22:00", "06:00", IST, changed=True, entry_seq=2)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (morning, night)),))
    limits = rr.EmployeeLimits(min_rest_hours=rr.Limit(11, rr.BLOCK, "rest.min_between_shifts_hours"))
    results = rr.validate(schedule, limits, rr.RuleContext())
    rest = of(results, rr.MIN_REST_VIOLATION)
    assert len(rest) == 1
    assert (rest[0].severity, rest[0].measured, rest[0].limit, rest[0].unit) == (rr.BLOCK, Decimal("8.00"), 11, "h")
    assert rest[0].dates == (D(2026, 11, 3),) and rest[0].employee_id == 1
    assert of(results, rr.SHIFT_OVERLAP) == []
    blocking, _, _ = rr.summarize(results, (), {}, {})
    assert rest[0] in blocking
    blocking, _, _ = rr.summarize(results, (), {rr.DOUBLE_SHIFT: "night cover"},
                                  {rr.MIN_REST_VIOLATION: {"exemption_ref": "EX-1", "reason": "breakdown"}})
    assert blocking == []


def test_six_9h_shifts_in_a_week_over_48_blocks():
    nine = V(start_time="09:00", end_time="18:00", shift_id=9)  # no unpaid break: 540 net
    shifts = tuple(work(nine, D(2026, 11, 2) + dt.timedelta(days=i), IST, changed=True) for i in range(6))
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, shifts),))
    for definition in (rr.FIXED, rr.ROLLING):
        limits = rr.EmployeeLimits(weekly_max_hours=rr.Limit(48, rr.BLOCK, "policy max_hours_per_week"),
                                   week_definition=definition, week_start_day=0)
        weekly = of(rr.validate(schedule, limits, rr.RuleContext()), rr.WEEKLY_HOURS_EXCEEDED)
        assert len(weekly) == 1, definition
        assert (weekly[0].severity, weekly[0].measured, weekly[0].limit) == (rr.BLOCK, Decimal("54.00"), 48)
        assert weekly[0].dates == tuple(D(2026, 11, 2) + dt.timedelta(days=i) for i in range(6))


# --------------------------------------------------------------------------- GV-AS-ROS


def test_gv_as_ros_02_pune_november_publish():
    schedule = pune_roster()
    assert sum(len(e.instances) for e in schedule.employees) == 1200
    assert sum(1 for e in schedule.employees for i in e.instances if i.kind == "WORK") == 900
    publish_at = at(IST, 2026, 10, 15, 11)
    context = rr.RuleContext(today=D(2026, 10, 15), publish_at=publish_at, consents=(SUNITA_CONSENT,),
                             coverage_rules=PUNE_COVERAGE, pattern_lints=tuple(rot.lint(PUNE_PATTERN, PUNE_SHIFTS)))
    results = rr.validate(schedule, rr.LimitsData(default=in_draft_limits()), context)
    assert keys(results, rr.BLOCK) == set()
    assert keys(results) == {rot.LINT_WEEKLY_ROTATION_SPEED, rot.LINT_CONSECUTIVE_LONG_OR_NIGHT,
                             rot.LINT_EARLY_START}
    assert keys(results, rr.WARN) == keys(results)
    # earliest instance Sun 2026-11-01 06:00 IST (CREW-B, A) -> 403.00 h (WORK instances, as §14.1 states)
    assert rr.min_notice_hours(schedule, publish_at, work_only=True) == Decimal("403.00")
    blocking, unresolved, missing = rr.summarize(results, sorted(keys(results)), {}, {})
    assert (blocking, unresolved, missing) == ([], [], [])


def test_gv_as_ros_02_variant_without_sunita_consent_flags_her_night_dates():
    context = rr.RuleContext(today=D(2026, 10, 15), publish_at=at(IST, 2026, 10, 15, 11))
    results = rr.validate(rr.ScheduleData((pune_employee(SUNITA, "C"),)), in_draft_limits(), context)
    night = of(results, rr.NIGHT_WORK_CONSENT_MISSING)
    assert night and {r.severity for r in night} == {rr.REQUIRE_REASON}
    assert {r.employee_id for r in night} == {SUNITA}


def test_gv_as_ros_03_shutdown_on_17_november():
    publish_at = at(IST, 2026, 11, 5, 11)
    shutdown = D(2026, 11, 17)

    def to_off(p):
        old = instance(p.shift, p.date)
        return off(p.date, changed=True, previous_kind="WORK", previous_start_at=old.start_at,
                   previous_net_minutes=old.scheduled_net_minutes)

    schedule = pune_roster(changed=False, edits={"B": {shutdown: to_off}})
    leave = rr.LeaveImpact(RAVI, "LR-RAVI-CL", (D(2026, 11, 16), D(2026, 11, 17), D(2026, 11, 18), D(2026, 11, 19)),
                           Decimal("2.0"), Decimal("1.0"))
    context = rr.RuleContext(today=D(2026, 11, 5), publish_at=publish_at, consents=(SUNITA_CONSENT,),
                             coverage_rules=PUNE_COVERAGE, affected_leave=(leave,))
    results = rr.validate(schedule, rr.LimitsData(default=in_draft_limits()), context)
    coverage = of(results, rr.COVERAGE_BELOW_MINIMUM)
    assert [(r.dates, r.details["shift_id"], r.measured, r.limit, r.severity) for r in coverage] == \
        [((shutdown,), SHIFT_C, 0, 9, rr.WARN)]
    affected = of(results, rr.ROSTER_AFFECTS_APPROVED_LEAVE)
    assert [(r.employee_id, r.limit, r.measured, r.severity) for r in affected] == \
        [(RAVI, Decimal("2.0"), Decimal("1.0"), rr.WARN)]
    assert keys(results, rr.BLOCK) == set()
    changed = [i for e in schedule.employees for i in e.instances if i.changed]
    assert len(changed) == 10
    assert {rr.notice_hours(rr.affected_start(i, IST), publish_at) for i in changed} == {Decimal("277.00")}


def test_gv_as_ros_04_kiran_substitution_rolling_vs_fixed_week():
    second_fourth_saturdays = {D(2026, 11, 14), D(2026, 11, 28), D(2026, 12, 12)}
    items = []
    for n in range((D(2026, 12, 13) - D(2026, 11, 16)).days + 1):
        day = D(2026, 11, 16) + dt.timedelta(days=n)
        if day == D(2026, 11, 25):
            items.append(off(day, changed=True, previous_kind="WORK"))
        elif day == D(2026, 11, 29):
            items.append(work(GEN, day, IST, changed=True, previous_kind="OFF"))
        elif day.weekday() == 6 or day in second_fourth_saturdays:
            items.append(off(day))
        else:
            items.append(work(GEN, day, IST))
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1101, IST, tuple(items)),))
    rest_only = dict(weekly_max_hours=None, weekly_rest_min_hours=None, night_work_groups={})
    rolling = of(rr.validate(schedule, in_draft_limits(**rest_only), rr.RuleContext()), rr.WEEKLY_REST_EXCEEDED)
    assert [(r.details["check"], r.measured, r.limit, r.severity) for r in rolling] == \
        [("DAYS_PER_WEEK", 7, 6, rr.WARN)]  # run 7 <= 10 passes
    assert rolling[0].dates == tuple(D(2026, 11, 29) + dt.timedelta(days=i) for i in range(7))
    fixed = rr.validate(schedule, in_draft_limits(week_definition=rr.FIXED, week_start_day=0, **rest_only),
                        rr.RuleContext())
    assert of(fixed, rr.WEEKLY_REST_EXCEEDED) == []  # weeks give 5 and 6
    # once the IN pack is VERIFIED the same result BLOCKs
    verified = in_draft_limits(max_days_per_week=rr.Limit(6, rr.BLOCK, verified=True), **rest_only)
    assert {r.severity for r in of(rr.validate(schedule, verified, rr.RuleContext()), rr.WEEKLY_REST_EXCEEDED)} == \
        {rr.BLOCK}
    # the substitution request itself (422 SUBSTITUTION_INVALID details.rule)
    assert rr.substitution_window(D(2026, 11, 29)) == (D(2026, 11, 16), D(2026, 12, 6))
    assert rr.substitution_issue(D(2026, 11, 29), D(2026, 11, 25), work_date_is_rest_day=True,
                                 off_date_is_working=True) is None
    assert rr.substitution_issue(D(2026, 11, 29), D(2026, 12, 9), work_date_is_rest_day=True,
                                 off_date_is_working=True) == rr.SUB_OUTSIDE_WINDOW


def _jordan_week(thursday=None, extra=()):
    days = [D(2026, 11, 1) + dt.timedelta(days=i) for i in range(8)]  # Sun 1 .. Sun 8
    items = []
    for day in days:
        if day == D(2026, 11, 5):
            items.append(thursday or work(NY_CLOSE, day, NY))
        elif day.weekday() in (5, 6) and day != D(2026, 11, 1):
            items.append(off(day))
        else:
            items.append(work(NY_OPEN, day, NY))
    return rr.ScheduleData((rr.EmployeeSchedule(4101, NY, tuple(items) + tuple(extra)),))


NY_LIMITS = rr.EmployeeLimits(
    min_rest_hours=rr.Limit(11, rr.WARN, "SCHEDULING_POLICY.min_rest_hours"),
    advance_notice_hours=rr.Limit(72, rr.REQUIRE_REASON, "WT-US-NY/NYC scheduling.advance_notice_hours", False),
    late_additions_require_consent=rr.Limit(True, rr.REQUIRE_REASON, verified=False),
    cancellation_notice_hours=rr.Limit(72, rr.REQUIRE_REASON, verified=False),
)


def test_gv_as_ros_06_jordan_close_then_open_rest():
    results = rr.validate(_jordan_week(), NY_LIMITS, rr.RuleContext())
    rest = of(results, rr.MIN_REST_VIOLATION)
    assert [(r.dates, r.measured, r.limit, r.severity) for r in rest] == \
        [((D(2026, 11, 5), D(2026, 11, 6)), Decimal("10.50"), 11, rr.WARN)]


def test_gv_as_ros_01_change_inside_72h_notice():
    old = se.build_instance(NY_CLOSE, D(2026, 11, 5), NY)
    moved = custom(D(2026, 11, 5), "14:00", "22:30", NY, breaks=(floating("17:00", "19:00", 30, False),),
                   changed=True, previous_kind="WORK",
                   previous_start_at=old.start_at, previous_net_minutes=old.scheduled_net_minutes)
    context = rr.RuleContext(publish_at=at(NY, 2026, 11, 2, 18))
    results = rr.validate(_jordan_week(thursday=moved), NY_LIMITS, context)
    notice = of(results, rr.NOTICE_PERIOD_SHORT)
    assert [(r.dates, r.measured, r.severity) for r in notice] == [((D(2026, 11, 5),), Decimal("67.50"),
                                                                    rr.REQUIRE_REASON)]
    assert [r.measured for r in of(results, rr.MIN_REST_VIOLATION)] == [Decimal("10.00")]
    assert of(results, rr.LATE_ADDITION_NO_CONSENT) == []  # same hours: not an addition
    # an employee-initiated change (publication kind REQUEST) raises no notice result (SP-AS-044)
    request = rr.RuleContext(publish_at=at(NY, 2026, 11, 2, 18), publication_kind="REQUEST", initiated_by=rr.EMPLOYEE)
    assert of(rr.validate(_jordan_week(thursday=moved), NY_LIMITS, request), rr.NOTICE_PERIOD_SHORT) == []


def test_gv_as_ros_05_late_addition_with_and_without_consent():
    sunday = custom(D(2026, 11, 8), "10:00", "14:00", NY, changed=True, entry_seq=1)
    schedule = _jordan_week()
    emp = schedule.employees[0]
    items = tuple(i for i in emp.instances if i.shift_date != D(2026, 11, 8)) + (sunday,)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(emp.employee_id, NY, items),))
    context = rr.RuleContext(publish_at=at(NY, 2026, 11, 6, 12))
    results = rr.validate(schedule, NY_LIMITS, context)
    assert [r.measured for r in of(results, rr.NOTICE_PERIOD_SHORT)] == [Decimal("46.00")]
    assert len(of(results, rr.LATE_ADDITION_NO_CONSENT)) == 1
    consent = rr.Consent(4101, rr.SCHEDULE_CHANGE_ACCEPTED, scope=rr.PER_INSTANCE, subject_date=D(2026, 11, 8))
    with_consent = rr.validate(schedule, NY_LIMITS, rr.RuleContext(publish_at=context.publish_at, consents=(consent,)))
    assert of(with_consent, rr.LATE_ADDITION_NO_CONSENT) == []
    assert len(of(with_consent, rr.NOTICE_PERIOD_SHORT)) == 1


def test_gv_as_ros_05_variant_withdrawal_inside_notice():
    friday = work(NY_OPEN, D(2026, 11, 6), NY, changed=True, withdrawn=True)
    schedule = _jordan_week()
    emp = schedule.employees[0]
    items = tuple(i for i in emp.instances if i.shift_date != D(2026, 11, 6)) + (friday,)
    results = rr.validate(rr.ScheduleData((rr.EmployeeSchedule(4101, NY, items),)), NY_LIMITS,
                          rr.RuleContext(publish_at=at(NY, 2026, 11, 4, 12)))
    assert [r.measured for r in of(results, rr.CANCELLATION_INSIDE_NOTICE)] == [Decimal("44.50")]
    assert [r.measured for r in of(results, rr.NOTICE_PERIOD_SHORT)] == [Decimal("44.50")]
    assert of(results, rr.MIN_REST_VIOLATION) == []  # the withdrawn Friday no longer follows the close


def test_gv_as_ros_07_sunita_night_consent_withdrawn_18_november():
    withdrawn = rr.Consent(SUNITA, rr.NIGHT_WORK, valid_from=D(2026, 1, 1), valid_to=D(2026, 11, 17))
    context = rr.RuleContext(publish_at=at(IST, 2026, 11, 16, 10), consents=(withdrawn,), check_span_days=None)
    results = rr.validate(rr.ScheduleData((pune_employee(SUNITA, "C"),)), in_draft_limits(), context)
    night = of(results, rr.NIGHT_WORK_CONSENT_MISSING)
    expected = [D(2026, 11, d) for d in range(22, 28)] + [D(2026, 11, 30)]  # B 22-27, C 30
    assert sorted(d for r in night for d in r.dates) == expected
    assert {r.severity for r in night} == {rr.REQUIRE_REASON}
    flagged = {d for r in night for d in r.dates}
    assert D(2026, 11, 18) not in flagged and D(2026, 11, 19) not in flagged  # A 06:00-14:00
    b_22 = next(r for r in night if r.dates == (D(2026, 11, 22),))
    assert b_22.measured == Decimal("3.00")  # B 14:00-22:00 overlaps 19:00-06:00 by 3 h


def test_gv_as_ros_10_notice_days_dst_safe():
    def one(start, publish, limits):
        item = work(NY_OPEN, start, NY, changed=True)
        schedule = rr.ScheduleData((rr.EmployeeSchedule(1, NY, (item,)),))
        return of(rr.validate(schedule, limits, rr.RuleContext(publish_at=publish)), rr.NOTICE_PERIOD_SHORT)

    days = rr.EmployeeLimits(advance_notice_days=rr.Limit(14, rr.REQUIRE_REASON),
                             advance_notice_hours=rr.Limit(336, rr.REQUIRE_REASON))
    hours_only = rr.EmployeeLimits(advance_notice_hours=rr.Limit(336, rr.REQUIRE_REASON))
    spring = (D(2027, 3, 15), at(NY, 2027, 3, 1, 8, 30))
    fall = (D(2026, 11, 2), at(NY, 2026, 10, 19, 8, 30))
    assert one(*spring, days) == [] and one(*fall, days) == []  # 14 calendar days, equality passes
    short = one(*spring, hours_only)
    assert [r.measured for r in short] == [Decimal("335.00")]
    assert one(*fall, hours_only) == []  # 337 h


def _sunita_rest_week(monday_start):
    items = [work(C, D(2026, 11, d), IST) for d in range(17, 22)]  # C Tue 17 .. Sat 21
    items.append(off(D(2026, 11, 22)))
    items.append(custom(D(2026, 11, 23), monday_start, "13:30" if monday_start == "05:30" else "14:00", IST,
                        changed=True))
    items += [work(A, D(2026, 11, d), IST) for d in range(24, 28)]
    return rr.ScheduleData((rr.EmployeeSchedule(SUNITA, IST, tuple(items)),))


def test_gv_as_ros_17_weekly_rest_hours_from_end_of_last_night():
    limits = in_draft_limits(night_work_groups={})
    early = rr.validate(_sunita_rest_week("05:30"), limits, rr.RuleContext())
    rest = of(early, rr.WEEKLY_REST_HOURS)
    assert [(r.measured, r.limit, r.severity) for r in rest] == [(Decimal("23.50"), 24, rr.WARN)]
    assert of(early, rr.WEEKLY_REST_EXCEEDED) == []  # the date-based check passes
    assert of(rr.validate(_sunita_rest_week("06:00"), limits, rr.RuleContext()), rr.WEEKLY_REST_HOURS) == []
    verified = in_draft_limits(night_work_groups={}, weekly_rest_min_hours=rr.Limit(24, rr.BLOCK))
    assert {r.severity for r in of(rr.validate(_sunita_rest_week("05:30"), verified, rr.RuleContext()),
                                   rr.WEEKLY_REST_HOURS)} == {rr.BLOCK}


def test_gv_as_ros_17_dst_measures_23_or_25_elapsed_hours():
    night = V(start_time="22:00", end_time="06:00", shift_id=7)
    day = V(start_time="06:00", end_time="14:00", shift_id=8)
    limits = rr.EmployeeLimits(weekly_rest_min_hours=rr.Limit(24, rr.WARN))

    def week(first_night, free_day):
        items = [work(night, first_night + dt.timedelta(days=i), NY) for i in range(4)]
        items += [work(day, free_day + dt.timedelta(days=i), NY) for i in range(1, 5)]
        return rr.ScheduleData((rr.EmployeeSchedule(1, NY, tuple(items)),))

    spring = of(rr.validate(week(D(2027, 3, 9), D(2027, 3, 13)), limits, rr.RuleContext()), rr.WEEKLY_REST_HOURS)
    assert [r.measured for r in spring] == [Decimal("23.00")]  # Sat 06:00 EST -> Sun 06:00 EDT
    fall = of(rr.validate(week(D(2026, 10, 27), D(2026, 10, 31)), limits, rr.RuleContext()), rr.WEEKLY_REST_HOURS)
    assert fall == []  # Sat 06:00 EDT -> Sun 06:00 EST = 25 h


def test_gv_as_ros_18_backdated_correction_blocks_until_exempted():
    friday = D(2026, 11, 20)
    old = instance(SHIFT_B, friday)
    corrected = off(friday, changed=True, previous_kind="WORK", previous_start_at=old.start_at)
    publish_at = at(IST, 2026, 12, 7, 11)
    context = rr.RuleContext(today=D(2026, 12, 7), publish_at=publish_at, period_states={friday: rr.FINALIZED})
    results = rr.validate(rr.ScheduleData((rr.EmployeeSchedule(RAVI, IST, (corrected,)),)), rr.EmployeeLimits(),
                          context)
    backdate = of(results, rr.BACKDATE_LIMIT)
    assert [(r.severity, r.dates, r.limit) for r in backdate] == [(rr.BLOCK, (friday,), 7)]
    assert [(r.severity, r.measured) for r in of(results, rr.PERIOD_LOCKED)] == [(rr.REQUIRE_REASON, rr.FINALIZED)]
    assert rr.notice_hours(rr.affected_start(corrected, IST), publish_at) == Decimal("-419.00")
    blocking, _, missing = rr.summarize(results, (), {rr.PERIOD_LOCKED: "payroll correction"},
                                        {rr.BACKDATE_LIMIT: {"exemption_ref": "EXM-7", "reason": "late info"}})
    assert blocking == [] and missing == []
    blocking, _, _ = rr.summarize(results, (), {}, {rr.BACKDATE_LIMIT: {"exemption_ref": " "}})
    assert [r.rule_key for r in blocking] == [rr.BACKDATE_LIMIT]


def test_gv_as_rot_09_joiner_backfill_backdate_requires_reason():
    days = [D(2026, 11, 2) + dt.timedelta(days=i) for i in range(16)]  # 2..17 Nov
    items = tuple(rr.from_shift_instance(instance(SHIFT_A, d), changed=True) for d in days)
    joiner = rr.EmployeeSchedule(4999, IST, items, employment_start=D(2026, 11, 2), joiner_backfill=True)
    results = of(rr.validate(rr.ScheduleData((joiner,)), rr.EmployeeLimits(), rr.RuleContext(today=D(2026, 11, 18))),
                 rr.BACKDATE_LIMIT)
    assert [(r.severity, r.dates[0], r.dates[-1]) for r in results] == \
        [(rr.REQUIRE_REASON, D(2026, 11, 2), D(2026, 11, 10))]  # D < 11 Nov; none for 11-17 Nov


def test_gv_as_ros_21_leaver_leftovers_never_block_an_unrelated_publish():
    swapped = rr.from_shift_instance(instance(SHIFT_B, D(2026, 11, 17)), changed=True)
    leftovers = tuple(rr.from_shift_instance(instance(SHIFT_C, D(2026, 11, d))) for d in range(21, 31))
    sunita = rr.EmployeeSchedule(SUNITA, IST, (swapped,) + leftovers, employment_end=D(2026, 11, 20))
    results = rr.validate(rr.ScheduleData((sunita,)), rr.EmployeeLimits(), rr.RuleContext(today=D(2026, 11, 16)))
    assert of(results, rr.NOT_EMPLOYED) == []
    late = rr.from_shift_instance(instance(SHIFT_C, D(2026, 11, 25)), changed=True)
    sunita = rr.EmployeeSchedule(SUNITA, IST, (swapped, late), employment_end=D(2026, 11, 20))
    blocked = of(rr.validate(rr.ScheduleData((sunita,)), rr.EmployeeLimits(), rr.RuleContext()), rr.NOT_EMPLOYED)
    assert [(r.severity, r.dates) for r in blocked] == [(rr.BLOCK, (D(2026, 11, 25),))]


# --------------------------------------------------------------------------- GV-AS-CTY (IN parts)


def test_gv_as_cty_01_sunita_night_shift_without_consent_in_draft_pack():
    night = rr.from_shift_instance(instance(SHIFT_C, D(2026, 11, 17)), changed=True)
    sunita = rr.EmployeeSchedule(SUNITA, IST, (night,), protected_groups=frozenset({"WOMEN"}))
    results = of(rr.validate(rr.ScheduleData((sunita,)), in_draft_limits(), rr.RuleContext()),
                 rr.NIGHT_WORK_CONSENT_MISSING)
    assert len(results) == 1
    r = results[0]
    assert (r.severity, r.dates, r.limit) == (rr.REQUIRE_REASON, (D(2026, 11, 17),), "19:00-06:00")
    assert r.source_ref == "WT-IN night_work.protected_groups[WOMEN]"
    assert r.message_key == "roster.rule.night_work_consent_missing.not_verified"  # "Compliance not verified"
    verified = in_draft_limits(night_work_groups={"WOMEN": rr.NightWorkRule(("19:00", "06:00"), True, rr.BLOCK)})
    assert of(rr.validate(rr.ScheduleData((sunita,)), verified, rr.RuleContext()),
              rr.NIGHT_WORK_CONSENT_MISSING)[0].severity == rr.BLOCK


def test_gv_as_cty_02_priya_11h_day_against_ka_10h():
    long_day = V(start_time="09:00", end_time="21:00", shift_id=55,
                 break_rules=(B("LUNCH", "FIXED_WINDOW", 60, False, start="13:00", end="14:00"),))
    priya = rr.EmployeeSchedule(5101, IST, (work(long_day, D(2026, 11, 4), IST, changed=True),))
    limits = rr.EmployeeLimits(daily_max_hours=rr.Limit(10, rr.WARN, "WT-IN-KA hours.daily_max_including_ot_hours",
                                                        verified=False))
    results = of(rr.validate(rr.ScheduleData((priya,)), limits, rr.RuleContext()), rr.DAILY_HOURS_EXCEEDED)
    assert [(r.severity, r.measured, r.limit, r.unit, r.source_ref) for r in results] == \
        [(rr.WARN, Decimal("11.00"), 10, "h", "WT-IN-KA hours.daily_max_including_ot_hours")]


def test_gv_as_cty_03_ot_cap_ms_shop_vs_pune_factory():
    usage = {D(2026, 10, 15): 3000, D(2026, 11, 15): 3000, D(2026, 12, 15): 3000}  # 150 h in Oct-Dec
    emp = rr.EmployeeSchedule(6101, IST, (work(GEN, D(2026, 11, 2), IST, changed=True),))
    context = rr.RuleContext(ot_minutes={6101: usage})
    shop = rr.EmployeeLimits(ot_caps=(rr.OtCap("QUARTER", Decimal(144), source_ref="WT-IN-MH overtime.caps[]"),))
    results = of(rr.validate(rr.ScheduleData((emp,)), shop, context), rr.OT_CAP_PROJECTED)
    assert [(r.severity, r.measured, r.limit, r.details["window_kind"]) for r in results] == \
        [(rr.WARN, Decimal("150.00"), Decimal(144), "CALENDAR")]
    factory = rr.EmployeeLimits()  # no matching pack cap: only the tenant's OVERTIME_POLICY applies (payroll)
    assert of(rr.validate(rr.ScheduleData((emp,)), factory, context), rr.OT_CAP_PROJECTED) == []


def test_gv_as_cty_16_rolling_quarter_only_exceeds():
    usage = {D(2026, 10, 10): 500, D(2026, 11, 15): 6000, D(2026, 12, 20): 1400, D(2027, 1, 10): 1600}
    emp = rr.EmployeeSchedule(2001, IST, (work(GEN, D(2027, 1, 11), IST, changed=True),))
    limits = rr.EmployeeLimits(ot_caps=(rr.OtCap("QUARTER", Decimal(144)),))
    results = of(rr.validate(rr.ScheduleData((emp,)), limits, rr.RuleContext(ot_minutes={2001: usage})),
                 rr.OT_CAP_PROJECTED)
    assert [(r.details["window_kind"], r.measured) for r in results] == [("ROLLING", Decimal("150.00"))]


# --------------------------------------------------------------------------- other rules


def test_overlap_integrity_only_on_changed_entries_and_touching_is_not_overlap():
    c_night = rr.from_shift_instance(instance(SHIFT_C, D(2026, 11, 2)), changed=True)  # ends 06:00 on 3 Nov
    a_morning = rr.from_shift_instance(instance(SHIFT_A, D(2026, 11, 3)), changed=True)  # starts 06:00
    touching = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (c_night, a_morning)),))
    assert of(rr.validate(touching, rr.EmployeeLimits(), rr.RuleContext()), rr.SHIFT_OVERLAP) == []
    late_c = custom(D(2026, 11, 3), "05:00", "09:00", IST, entry_seq=2, changed=True)
    overlapping = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (c_night, late_c)),))
    overlap = of(rr.validate(overlapping, rr.EmployeeLimits(), rr.RuleContext()), rr.SHIFT_OVERLAP)
    assert [(r.severity, r.measured, r.dates) for r in overlap] == [(rr.BLOCK, 60, (D(2026, 11, 2), D(2026, 11, 3)))]
    untouched = custom(D(2026, 11, 3), "05:00", "09:00", IST, entry_seq=2)
    published = rr.from_shift_instance(instance(SHIFT_C, D(2026, 11, 2)))
    quiet = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (published, untouched)),))
    assert of(rr.validate(quiet, rr.EmployeeLimits(), rr.RuleContext()), rr.SHIFT_OVERLAP) == []


def test_archived_double_shift_holiday_spread_and_on_call():
    split = V(kind="SPLIT", start_time="10:00", end_time="22:00", shift_id=60,
              segments=({"kind": "WORK", "start": "10:00", "end": "14:00", "next_day": False},
                        {"kind": "ON_CALL", "start": "14:00", "end": "18:00", "next_day": False},
                        {"kind": "WORK", "start": "18:00", "end": "22:00", "next_day": False}))
    holiday = D(2027, 1, 26)
    first = work(split, holiday, IST, changed=True, archived=True)
    second = custom(holiday, "23:00", "23:30", IST, changed=True, entry_seq=2)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, (first, second)),))
    limits = rr.EmployeeLimits(spread_over_max_hours=rr.Limit(12, rr.WARN), on_call_allowed=False,
                               holiday_mode=rr.HOLIDAY_OFF)
    results = rr.validate(schedule, limits, rr.RuleContext(holidays={holiday}))
    found = {r.rule_key: r for r in results}
    assert found[rr.SHIFT_ARCHIVED].severity == rr.BLOCK
    assert found[rr.DOUBLE_SHIFT].severity == rr.REQUIRE_REASON and found[rr.DOUBLE_SHIFT].measured == 2
    assert found[rr.HOLIDAY_WORK].severity == rr.REQUIRE_REASON
    assert found[rr.SPREAD_OVER_EXCEEDED].measured == Decimal("13.50")
    assert found[rr.ON_CALL_NOT_ALLOWED].severity == rr.WARN
    through = rr.validate(schedule, rr.EmployeeLimits(holiday_mode=rr.WORK_THROUGH), rr.RuleContext(holidays={holiday}))
    assert of(through, rr.HOLIDAY_WORK) == []
    raised = rr.EmployeeLimits(rule_severity={rr.DOUBLE_SHIFT: rr.BLOCK, rr.HOLIDAY_WORK: rr.WARN})
    found = {r.rule_key: r for r in rr.validate(schedule, raised, rr.RuleContext(holidays={holiday}))}
    assert found[rr.DOUBLE_SHIFT].severity == rr.BLOCK  # raise allowed
    assert found[rr.HOLIDAY_WORK].severity == rr.REQUIRE_REASON  # never lowered


def test_compressed_week_consent_lifts_daily_hours():
    """FOUR_ON_THREE_OFF_12H_IN: 12-h dates raise DAILY_HOURS_EXCEEDED unless compressed week + consent."""
    twelve = V(start_time="07:00", end_time="19:00", shift_id=12)
    emp = rr.EmployeeSchedule(1, IST, tuple(work(twelve, D(2026, 11, 2) + dt.timedelta(days=i), IST, changed=True)
                                            for i in range(4)))
    schedule = rr.ScheduleData((emp,))
    limits = rr.EmployeeLimits(daily_max_hours=rr.Limit(10, rr.WARN), compressed_week_allowed=True)
    assert len(of(rr.validate(schedule, limits, rr.RuleContext()), rr.DAILY_HOURS_EXCEEDED)) == 4
    consent = rr.Consent(1, rr.COMPRESSED_WEEK, valid_from=D(2026, 11, 1))
    assert of(rr.validate(schedule, limits, rr.RuleContext(consents=(consent,))), rr.DAILY_HOURS_EXCEEDED) == []


def test_rest_waiver_consent_turns_clopening_into_require_reason():
    close = custom(D(2026, 11, 5), "13:30", "22:00", NY, changed=True)
    opening = custom(D(2026, 11, 6), "06:00", "14:00", NY, changed=True)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, NY, (close, opening)),))
    limits = rr.EmployeeLimits(min_rest_hours=rr.Limit(11, rr.BLOCK), rest_waivable_with_consent=True)
    assert of(rr.validate(schedule, limits, rr.RuleContext()), rr.MIN_REST_VIOLATION)[0].severity == rr.BLOCK
    waiver = rr.Consent(1, rr.REST_WAIVER, scope=rr.PER_INSTANCE, subject_date=D(2026, 11, 6), consent_id=5)
    rest = of(rr.validate(schedule, limits, rr.RuleContext(consents=(waiver,))), rr.MIN_REST_VIOLATION)[0]
    assert (rest.severity, rest.details["consent_id"], rest.measured) == (rr.REQUIRE_REASON, 5, Decimal("8.00"))


def test_rotation_coverage_and_tagged_coverage_rules():
    schedule = pune_roster(changed=True)
    crews = {f"CREW-{c}": frozenset(CREW_MEMBERS[c]) for c in CREWS}
    rotation = rr.RotationCoverage("PUNE-6ON2OFF", crews, {SHIFT_A: 1, SHIFT_B: 1, SHIFT_C: 1})
    tagged = rr.CoverageRule(SHIFT_C, 1, required_tags=("FORKLIFT",), from_date=D(2026, 11, 2), to_date=D(2026, 11, 2))
    tags = {CREW_MEMBERS["D"][0]: (rr.TagHolding("FORKLIFT", valid_to=D(2026, 10, 31)),)}  # expired
    context = rr.RuleContext(rotation_coverage=(rotation,), coverage_rules=(tagged,), employee_tags=tags)
    results = of(rr.validate(schedule, rr.EmployeeLimits(lints_enabled=False), context), rr.COVERAGE_BELOW_MINIMUM)
    assert [(r.dates, r.measured, r.limit) for r in results] == [((D(2026, 11, 2),), 0, 1)]  # rotation coverage holds


def test_check_span_keeps_only_results_near_changed_dates():
    nine = V(start_time="09:00", end_time="18:00", shift_id=9)
    early = tuple(work(nine, D(2026, 11, 2) + dt.timedelta(days=i), IST) for i in range(6))  # 54 h, untouched
    later = (work(nine, D(2026, 12, 21), IST, changed=True),)
    schedule = rr.ScheduleData((rr.EmployeeSchedule(1, IST, early + later),))
    limits = rr.EmployeeLimits(weekly_max_hours=rr.Limit(48, rr.WARN), week_definition=rr.FIXED)
    assert of(rr.validate(schedule, limits, rr.RuleContext()), rr.WEEKLY_HOURS_EXCEEDED) == []
    everything = rr.RuleContext(check_span_days=None)
    assert len(of(rr.validate(schedule, limits, everything), rr.WEEKLY_HOURS_EXCEEDED)) == 1


def test_publish_lints_backward_rotation_on_transfer_seam():
    """§14.2(b): Imran's B on Sat 21 followed by A on Mon 23 -> LINT_BACKWARD_ROTATION; rest 32 h passes."""
    # CREW-D 2-22 Nov: C 2-5, OFF, A 8-13, OFF, B 16-21, OFF 22; then CREW-B's A from Mon 23
    old = pune_employee(4004, "D", start=D(2026, 11, 2), end=D(2026, 11, 22), changed=False).instances
    items = old + (rr.from_shift_instance(instance(SHIFT_A, D(2026, 11, 23)), changed=True),)
    limits = rr.EmployeeLimits(min_rest_hours=rr.Limit(11, rr.WARN))
    results = rr.validate(rr.ScheduleData((rr.EmployeeSchedule(4004, IST, items),)), limits, rr.RuleContext())
    backward = of(results, rot.LINT_BACKWARD_ROTATION)
    assert [r.dates for r in backward] == [(D(2026, 11, 21), D(2026, 11, 23))]
    assert of(results, rr.MIN_REST_VIOLATION) == []
    muted = rr.EmployeeLimits(lints_muted=frozenset({rot.LINT_BACKWARD_ROTATION}))
    assert of(rr.validate(rr.ScheduleData((rr.EmployeeSchedule(4004, IST, items),)), muted, rr.RuleContext()),
              rot.LINT_BACKWARD_ROTATION) == []


def test_summarize_resolution_rules():
    warn = rr.RuleResult(rot.LINT_EARLY_START, rr.WARN, 1)
    reason = rr.RuleResult(rr.NOTICE_PERIOD_SHORT, rr.REQUIRE_REASON, 1)
    block = rr.RuleResult(rr.SHIFT_OVERLAP, rr.BLOCK, 1)
    results = [warn, reason, block]
    assert rr.summarize(results, (), {}, {}) == ([block], [warn], [reason])
    assert rr.summarize(results, [rot.LINT_EARLY_START], {rr.NOTICE_PERIOD_SHORT: "  "}, {}) == ([block], [], [reason])
    assert rr.summarize(results, [rot.LINT_EARLY_START], {rr.NOTICE_PERIOD_SHORT: "coverage gap"},
                        {rr.SHIFT_OVERLAP: {"exemption_ref": "EX-9", "reason": "x"}}) == ([], [], [])
    assert rr.RuleResult is rot.RuleResult
    assert warn.to_dict()["severity"] == "WARN"


def test_dst_night_durations_use_utc_instants():
    """§6.1 Time: an 8-h C night in New York is 9 h on the fall-back night (net minutes come from the instance)."""
    fall = work(V(start_time="22:00", end_time="06:00", shift_id=7), D(2026, 10, 31), NY)
    assert fall.gross_minutes == 9 * 60
    assert pytz.timezone(NY).utcoffset(dt.datetime(2026, 11, 2)) == dt.timedelta(hours=-5)
