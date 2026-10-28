"""Dynamic settings ON/OFF harness (Goal 2 §6): the 13-step cycle for every optional payroll feature.

For each feature the product implements, through the real APIs (settings PUT/resolve, policies, inputs, runs:
create -> precheck -> calculation job -> recalculation of the same draft):

  (1) inspect the effective default (resolve API)          (2) enable it in Settings (PUT values)
  (3) choose its policy (policies API)                     (4) seed valid, approved source data
  (5) calculate a draft run                                (6) assert the exact change, worked out by hand
  (7) disable                                              (8) recalculate the same, now stale, draft run
  (9) assert the documented disabled-state result          (10) re-enable with a different valid policy
  (11) assert the new exact result                         (12) override at another permitted level
  (13) assert the override wins only where it applies, and that another tenant's values and run are unchanged.

Expected amounts are written out by hand in each test (Decimal arithmetic with the reasoning in comments);
nothing calls the engine or a service to produce an expectation. World (``settings_world``, conftest.py): BASIC =
1,000.00 GBP x N for a month of N calendar days (CTC_BASED structure, BASIC = ctc.monthly x 100%, tagged
LOP_APPLICABLE and OT_BASE), so under CALENDAR_DAYS one day of LOP is 1,000.00 and the default overtime hourly
rate SUM_TAG_RATE("OT_BASE") / N / 8 (written explicitly into every overtime policy, OT_RATE_FORMULA, so the
expectations never depend on the policy default) is 1,000.00 x N / N / 8 = 125.00 GBP. The tenants use the MON_FRI
work week.

Setup shortcuts that are not calculation shortcuts (documented where used): a loan is disbursed through
``loans.record_disbursement`` (the payment-evidence hook; the API path is an off-cycle disbursement run with
step-up finalization) and bank accounts are stored with the product tests' helper (the API path imposes a 48 h
cooling-off).

Pairwise interactions: attendance x leave (3.1 §6.1) and holiday premium x weekly-off premium x overtime
(3.1 §6.2). ``BLOCKED_FEATURES`` lists what cannot be verified through a run, with the precise reason.
"""

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

from tests.payroll.verification.conftest import (
    DAY_RATE,
    PAY,
    activate_policy,
    approve_request,
    calculate,
    create_run,
    days_between,
    employee_lines,
    employee_login,
    idem,
    monthly_basic,
    ok,
    open_findings,
    precheck,
    put_setting,
    recalc,
    resolve,
    run_detail,
    second_admin,
    today_of,
)
from tests.payroll.test_bank_accounts import bank_world, world  # noqa: F401  (fixtures of the bank alert cycle)

D = Decimal
CENT = D("0.01")
OT_RATE = D("125.00")          # 1,000.00 x N / N / 8 standard hours (see module docstring)
REASON = "Verification: ON/OFF cycle"


def r2(value):
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute)).isoformat() + "+00:00"


def past_weekdays(w):
    today = today_of(w["admin"])
    days = [d for d in days_between(w["start"], min(today, w["end"])) if d.weekday() < 5]
    assert len(days) >= 9, f"the harness needs 9 past working days in the period, found {days}"
    return days


def past_weekend(w):
    today = today_of(w["admin"])
    days = [d for d in days_between(w["start"], min(today, w["end"])) if d.weekday() >= 5]
    assert days, "the harness needs a past weekly off in the period"
    return days


def leave_weekdays(w, count=3):
    """Working days of the period for leave: the future ones while enough remain, otherwise the period's last
    ``count`` weekdays (booked ahead by ``apply_leave``). Never the first weekday, which holds a past absence."""
    today = today_of(w["admin"])
    future = [d for d in days_between(today + timedelta(days=1), w["end"]) if d.weekday() < 5]
    if len(future) >= count:
        return future
    weekdays = [d for d in days_between(w["start"], w["end"]) if d.weekday() < 5]
    assert len(weekdays) > count, f"the period needs more than {count} weekdays, found {weekdays}"
    return weekdays[-count:]


@contextmanager
def booked_before(w, start):
    """A leave that starts in the past is booked as it would have been: with the app clock (``clock.utcnow``, not
    session or token time) on the day before it. Late in a month the period has too few future working days for
    the cycle, and leave can never start in the past (LH BUG-07)."""
    if start >= today_of(w["admin"]):
        yield
        return
    from app.core import clock

    instant = datetime.combine(start - timedelta(days=1), time(6, 0), tzinfo=timezone.utc)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(clock, "utcnow", lambda: instant)
        yield


def weekday_count(w):
    """W: scheduled working days of the period (Mon-Fri, no holidays unless a test adds one)."""
    return sum(1 for d in days_between(w["start"], w["end"]) if d.weekday() < 5)


def attendance(w, person, day, status="present", start=9, end=17, next_day=False):
    body = {"employee_id": w["emp"][person], "date": day.isoformat(), "check_in_time": at(day, start),
            "status": status}
    if end is not None:
        body["check_out_time"] = at(day + timedelta(days=1) if next_day else day, end)
    return ok(w["admin"].post("/api/v1/attendance/manual", json=body))


def on_duty(w, person, day):
    created = ok(w["admin"].post(f"{PAY}/on-duty", json={"employee_id": w["emp"][person], "date": day.isoformat(),
                                                          "reason": "Approved work on a non-working day"},
                                 headers=idem()), 201)
    approve_request(w, created["approval_request_id"], deciders=("manager", "approver", "second_admin"))


def switch(w, key, value, scope_type="TENANT", scope_id=None, effective_from=None):
    """PUT a switch effective from the period start (past date: a reason is required and given)."""
    day = effective_from or w["start"]
    return put_setting(w["admin"], key, value, scope_type, scope_id, effective_from=day,
                       reason=REASON if day < today_of(w["admin"]) else None)


def lines_of(w, run_id, person):
    lines, detail = employee_lines(w["admin"], run_id, w["emp"][person])
    return lines, detail


def basic(w, run_id, person):
    return lines_of(w, run_id, person)[0].get("BASIC")


def amount(w, run_id, person, component):
    return lines_of(w, run_id, person)[0].get(component)


def findings_of(w, run_id, code):
    return [f for f in open_findings(w["admin"], run_id) if f["code"] == code]


def fresh_run(w, group_code):
    run = create_run(w["admin"], w["groups"][group_code], w["periods"][group_code]["id"])
    precheck(w["admin"], run["id"])
    calculate(w["admin"], run["id"])
    return run["id"]


def recalculated(w, run_id, expect_stale=True):
    """Step 8: the setting change marked the locked draft stale; recalculate it."""
    if expect_stale is not None:
        assert run_detail(w["admin"], run_id)["stale"] is expect_stale, run_detail(w["admin"], run_id)
    recalc(w["admin"], run_id)
    return run_id


def assert_bystander_unchanged(world, *keys):
    """Step 13: Verity Beta's resolved values and its recalculated run never move."""
    beta = world["beta"]
    for key, value in keys:
        found = resolve(beta["admin"], key, employee_id=beta["emp"]["Zed"], on=beta["end"])
        assert found["value"] == value, (key, found)
    recalc(beta["admin"], beta["run"])
    lines, detail = employee_lines(beta["admin"], beta["run"], beta["emp"]["Zed"])
    month = monthly_basic(beta["period"])
    assert lines == {"BASIC": month}, lines
    assert D(detail["totals"]["net"]["amount"]) == month


def describe(w, run_id, person):
    lines, detail = lines_of(w, run_id, person)
    return {"lines": {k: str(v) for k, v in lines.items()},
            "row": {k: detail.get(k) for k in ("status", "exclusion_reason", "exclusion_note", "error", "totals")},
            "ledger_totals": (detail.get("ledger") or {}).get("totals"),
            "findings": [(f["code"], f["message"][:120]) for f in open_findings(w["admin"], run_id)
                         if f.get("employee_id") in (None, w["emp"][person])]}


# ---------------------------------------------------------------------------
# Attendance pay (3.1 §5.2) and the half-day value
# ---------------------------------------------------------------------------
def test_attendance_pay_full_cycle(settings_world):
    w = settings_world["alpha"]
    month, wk = monthly_basic(w["period"]), weekday_count(w)
    wd = past_weekdays(w)
    attendance(w, "Ann", wd[0], "absent", end=None)              # a full LOP day
    attendance(w, "Ann", wd[1], "half-day", 9, 13)               # half paid at the default value 0.5
    attendance(w, "Ann", wd[2])                                  # present
    attendance(w, "Ben", wd[0])
    attendance(w, "Cal", wd[0])
    key = "payroll.attendance.enabled"
    # (1) effective default: the world's baseline switched attendance OFF at TENANT level.
    found = resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])
    assert (found["value"], found["level"]) == (False, "TENANT")
    # (2)-(3) policy: unmarked working days count as present; switch ON.
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, key, True)
    # (5)-(6) Ann loses 1 (absent) + 0.5 (half day) = 1.5 days of 1,000.00; Ben none.
    run_id = fresh_run(w, "PG-A")
    assert basic(w, run_id, "Ann") == month - D("1500.00"), describe(w, run_id, "Ann")
    assert basic(w, run_id, "Ben") == month
    # (7)-(9) OFF: punches never change pay.
    switch(w, key, False)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month and basic(w, run_id, "Ben") == month
    # (10)-(11) ON with another policy: unmarked working days are absences.
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "ABSENT"})
    switch(w, key, True)
    recalculated(w, run_id)
    # Ann: absent 1 + half 0.5 + unmarked (W - 3 recorded days) = W - 1.5 LOP days.
    assert basic(w, run_id, "Ann") == month - DAY_RATE * (wk - D("1.5")), describe(w, run_id, "Ann")
    # Ben: present on one day, the other W - 1 working days unmarked -> absent.
    assert basic(w, run_id, "Ben") == month - DAY_RATE * (wk - 1)
    # (12) employee-level policy override for Ben (priority 10 above the catch-all): unmarked -> present.
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"}, name="Ben is presumed present",
                    criteria={"employee_ids": [w["emp"]["Ben"]]}, priority=10)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ben") == month                                       # the override applies to Ben
    assert basic(w, run_id, "Ann") == month - DAY_RATE * (wk - D("1.5"))          # but not to Ann
    # (12) pay-group override: attendance OFF for PG-A only.
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month and basic(w, run_id, "Ben") == month
    cal_run = fresh_run(w, "PG-B")                                                # PG-B follows the tenant: ON
    assert basic(w, cal_run, "Cal") == month - DAY_RATE * (wk - 1), describe(w, cal_run, "Cal")
    assert resolve(w["admin"], key, employee_id=w["emp"]["Cal"], on=w["end"])["level"] == "TENANT"
    # (13)
    assert_bystander_unchanged(settings_world, (key, False))


def test_half_day_value_cycle(settings_world):
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    wd = past_weekdays(w)
    attendance(w, "Ann", wd[1], "half-day", 9, 13)
    attendance(w, "Cal", wd[1], "half-day", 9, 13)
    key = "payroll.attendance.half_day_value"
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, "payroll.attendance.enabled", True)
    assert D(resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])["value"]) == D("0.5")
    run_id = fresh_run(w, "PG-A")
    assert basic(w, run_id, "Ann") == month - D("500.00")          # half of the day unpaid
    switch(w, key, "0")                                             # a half day is worth nothing
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("1000.00")
    switch(w, key, "1", "PAY_GROUP", w["groups"]["PG-A"])           # PG-A pays half days in full
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    cal_run = fresh_run(w, "PG-B")                                  # tenant value 0 still applies to PG-B
    assert basic(w, cal_run, "Cal") == month - D("1000.00")
    assert_bystander_unchanged(settings_world, (key, "0.5"))


def test_sandwich_rule_cycle(settings_world):
    """payroll.lop.sandwich_rule (3.1 §5.2): a weekly off between two full LOP working days becomes LOP too."""
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    wd = past_weekdays(w)
    # A Friday and the Monday right after it, both past and inside the period.
    pairs = [(f, m) for f in wd for m in wd if f.weekday() == 4 and m == f + timedelta(days=3)]
    assert pairs, f"the harness needs a past Friday and the following Monday in the period: {wd}"
    friday, monday = pairs[0]
    for person in ("Ann", "Cal"):
        attendance(w, person, friday, "absent", end=None)
        attendance(w, person, monday, "absent", end=None)
    key = "payroll.lop.sandwich_rule"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])["value"] == "OFF"
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, "payroll.attendance.enabled", True)
    run_id = fresh_run(w, "PG-A")
    # OFF: only the two absent working days are LOP -> 2 x 1,000.00.
    assert basic(w, run_id, "Ann") == month - D("2000.00"), describe(w, run_id, "Ann")
    # WEEKLY_OFFS: Saturday and Sunday sit between two full LOP days -> 4 x 1,000.00.
    switch(w, key, "WEEKLY_OFFS")
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("4000.00"), describe(w, run_id, "Ann")
    # HOLIDAYS only: the weekend is not a holiday -> back to 2 days.
    switch(w, key, "HOLIDAYS")
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("2000.00")
    # Pay-group override: BOTH for PG-A only; PG-B follows the tenant (HOLIDAYS) -> Cal loses 2 days.
    switch(w, key, "BOTH", "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("4000.00")
    cal_run = fresh_run(w, "PG-B")
    assert basic(w, cal_run, "Cal") == month - D("2000.00"), describe(w, cal_run, "Cal")
    assert_bystander_unchanged(settings_world, (key, "OFF"))


# ---------------------------------------------------------------------------
# Leave pay (3.1 §5.3) x attendance (3.1 §6.1)
# ---------------------------------------------------------------------------
def apply_leave(w, person, leave_type, start, end=None):
    client = employee_login(w, person)  # signed in on the real clock
    with booked_before(w, start):
        leave = ok(client.post("/api/v1/leaves/apply", json={"leave_type": leave_type, "start_date": start.isoformat(),
                                                             "end_date": (end or start).isoformat()}))
        approved = ok(w["manager"].post(f"/api/v1/leaves/{leave['id']}/approve", json={}))
    assert approved["status"] == "approved", approved
    return leave["id"]


def test_leave_pay_cycle_with_the_attendance_pairs(settings_world):
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    fw = leave_weekdays(w, 3)
    key, attendance_key = "payroll.leave.enabled", "payroll.attendance.enabled"
    # (1) starter: leave ON with the leave-type policy (unpaid -> UNPAID, casual -> FULL).
    found = resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])
    assert found["value"] is True
    # (4) Ann: 2 unpaid days and 1 casual day, approved by her manager.
    apply_leave(w, "Ann", "unpaid", fw[0], fw[1])
    apply_leave(w, "Ann", "casual", fw[2])
    switch(w, key, True)
    # Attendance OFF x leave ON: paid unless an approved unpaid leave covers the day -> 2 LOP days.
    run_id = fresh_run(w, "PG-A")
    assert basic(w, run_id, "Ann") == month - D("2000.00"), describe(w, run_id, "Ann")
    # A manual LOP on a day already covered by leave is refused: a day is never deducted twice.
    r = w["admin"].post(f"{PAY}/lop-inputs", json={"employee_id": w["emp"]["Ann"], "date": fw[0].isoformat(),
                                                    "reason": "Double count attempt"}, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "LOP_SLOT_TAKEN", r.text
    # (7)-(9) Attendance OFF x leave OFF: every working day is paid.
    switch(w, key, False)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    # Attendance ON x leave OFF: approved leave is PAID_EXCUSED by default ...
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, attendance_key, True)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    # ... or ABSENT where configured: all 3 leave days become LOP.
    switch(w, "payroll.leave.when_disabled_treat_approved_leave_as", "ABSENT", "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("3000.00")
    # Attendance ON x leave ON, plus a past absence: unpaid leave 2 + absent 1 = 3; casual paid.
    attendance(w, "Ann", past_weekdays(w)[0], "absent", end=None)
    switch(w, key, True)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("3000.00"), describe(w, run_id, "Ann")
    # (10)-(11) another leave policy: casual paid at 50%. The treatment is frozen on each leave day at
    # approval (3.4 §7, leave_days.pay_treatment_snapshot): Ann's casual day stays FULL, Ben's new one is 50%.
    rows = [{"leave_type_code": "casual", "pay_treatment": "PARTIAL", "partial_percent": "50"},
            {"leave_type_code": "unpaid", "pay_treatment": "UNPAID"}]
    activate_policy(w["admin"], "LEAVE_PAY_POLICY", {"rows": rows})
    apply_leave(w, "Ben", "casual", fw[2])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("3000.00")
    assert basic(w, run_id, "Ben") == month - D("500.00"), describe(w, run_id, "Ben")
    # (12) pay-group override: leave OFF for PG-A (attendance still ON, approved leave PAID_EXCUSED... but PG-A
    # configured ABSENT above, so switch that back) -> Ann pays only her past absence, Ben is paid in full.
    switch(w, "payroll.leave.when_disabled_treat_approved_leave_as", "PAID_EXCUSED", "PAY_GROUP", w["groups"]["PG-A"])
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("1000.00") and basic(w, run_id, "Ben") == month
    apply_leave(w, "Cal", "unpaid", fw[0])                          # PG-B follows the tenant: leave ON
    cal_run = fresh_run(w, "PG-B")
    assert basic(w, cal_run, "Cal") == month - D("1000.00")
    assert_bystander_unchanged(settings_world, (key, True))


# ---------------------------------------------------------------------------
# Overtime (3.1 §5.5), DAY grain (EC-CFG-08)
# ---------------------------------------------------------------------------
def overtime(w, person, day, minutes):
    entry = ok(w["admin"].post("/api/v1/overtime/entries", json={
        "employee_id": w["emp"][person], "date": day.isoformat(), "minutes": minutes, "reason": "Release"},
        headers=idem()), 201)
    ok(w["manager"].post(f"/api/v1/overtime/entries/{entry['id']}/approve", json={}, headers=idem()))
    return entry["id"]


# Written out explicitly (never the policy default, which another change is revising): the hourly rate is the
# full-period OT_BASE (BASIC = 1,000.00 x N) / basis days (N under CALENDAR_DAYS) / 8 standard hours = 125.00.
OT_RATE_FORMULA = 'SUM_TAG_RATE("OT_BASE") / period.basis_days / emp.standard_hours_per_day'


def ot_fields(workday="1.5", weekly_off="2.0", holiday="3.0"):
    return {"source": "APPROVED_REQUESTS", "rate_formula": OT_RATE_FORMULA, "buckets": [
        {"day_type": "WORKDAY", "after_hours": "0", "multiplier": workday},
        {"day_type": "WEEKLY_OFF", "after_hours": "0", "multiplier": weekly_off},
        {"day_type": "HOLIDAY", "after_hours": "0", "multiplier": holiday}]}


def test_overtime_cycle_with_day_grain(settings_world):
    w = settings_world["alpha"]
    wd = past_weekdays(w)
    key = "payroll.overtime.enabled"
    overtime(w, "Ben", wd[0], 120)
    overtime(w, "Ann", wd[1], 60)
    overtime(w, "Cal", wd[0], 60)
    found = resolve(w["admin"], key, employee_id=w["emp"]["Ben"], on=w["end"])
    assert found["value"] is False                                  # starter: OFF, policy draft without multipliers
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="1.5"))
    switch(w, key, True)
    run_id = fresh_run(w, "PG-A")
    # Ben 2 h x 125.00 x 1.5 = 375.00; Ann 1 h x 125.00 x 1.5 = 187.50.
    assert amount(w, run_id, "Ben", "OT_WORKDAY") == D("375.00"), describe(w, run_id, "Ben")
    assert amount(w, run_id, "Ann", "OT_WORKDAY") == D("187.50")
    switch(w, key, False)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") is None and amount(w, run_id, "Ann", "OT_WORKDAY") is None
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="2.0"))
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") == D("500.00")    # 2 x 125.00 x 2.0
    assert amount(w, run_id, "Ann", "OT_WORKDAY") == D("250.00")    # 1 x 125.00 x 2.0
    # The same policy without a rate formula falls back to the product default SUM_TAG_HOURLY("OT_BASE"): BASIC
    # is a PERIOD amount (1,000.00 x N), so the hourly rate is 1,000.00 x N / N basis days / 8 h = 125.00 again
    # and the amounts are unchanged (2 x 125.00 x 2.0 = 500.00; 1 x 125.00 x 2.0 = 250.00), with no unit warning.
    default_formula = {k: v for k, v in ot_fields(workday="2.0").items() if k != "rate_formula"}
    activate_policy(w["admin"], "OVERTIME_POLICY", default_formula)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") == D("500.00"), describe(w, run_id, "Ben")
    assert amount(w, run_id, "Ann", "OT_WORKDAY") == D("250.00")
    warnings = [f for f in open_findings(w["admin"], run_id) if f.get("code", "").startswith("OT_BASE_UNITS")]
    assert warnings == [], warnings
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="2.0"))
    # DAY grain: ON only from the day after Ben's overtime -> Ben's hours are not paid, Ann's (wd[1]) are.
    put_setting(w["admin"], key, False, effective_from=w["start"], reason=REASON)
    put_setting(w["admin"], key, True, effective_from=wd[0] + timedelta(days=1), reason=REASON)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") is None, describe(w, run_id, "Ben")
    assert amount(w, run_id, "Ann", "OT_WORKDAY") == D("250.00")
    switch(w, key, True)
    # (12) employee policy for Ben (1.25) and a pay-group switch OFF for PG-A.
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="1.25"), name="Ben overtime",
                    criteria={"employee_ids": [w["emp"]["Ben"]]}, priority=10)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") == D("312.50")    # 2 x 125.00 x 1.25
    assert amount(w, run_id, "Ann", "OT_WORKDAY") == D("250.00")
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "OT_WORKDAY") is None and amount(w, run_id, "Ann", "OT_WORKDAY") is None
    cal_run = fresh_run(w, "PG-B")
    assert amount(w, cal_run, "Cal", "OT_WORKDAY") == D("250.00")   # the tenant ON still applies to PG-B
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Holiday premium x weekly-off premium x overtime (3.1 §6.2, I-16)
# ---------------------------------------------------------------------------
def test_holiday_and_weekly_off_premiums_pairwise_with_overtime(settings_world):
    w = settings_world["alpha"]
    wd, we = past_weekdays(w), past_weekend(w)
    holiday_day = wd[5]
    ok(w["admin"].post("/api/v1/holidays", json={"name": "Verification Day", "date": holiday_day.isoformat()}))
    attendance(w, "Ann", we[0], start=9, end=19)                   # 10 h on a weekly off
    attendance(w, "Ann", holiday_day, start=9, end=17)            # 8 h on the holiday
    on_duty(w, "Ann", we[0])
    on_duty(w, "Ann", holiday_day)
    attendance(w, "Cal", we[0], start=9, end=17)                  # 8 h on a weekly off (PG-B)
    on_duty(w, "Cal", we[0])
    premium, weekly, ot = ("payroll.holiday_work_premium.enabled", "payroll.weekly_off_pay.enabled",
                           "payroll.overtime.enabled")
    for k in (premium, weekly, ot):
        assert resolve(w["admin"], k, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is False
    activate_policy(w["admin"], "HOLIDAY_PAY_POLICY", {"holiday_work_multiplier": "1.0",
                                                        "weekly_off_work_multiplier": "0.5"})
    activate_policy(w["admin"], "OVERTIME_POLICY", ot_fields(workday="1.5", weekly_off="2.0", holiday="3.0"))
    run_id = fresh_run(w, "PG-A")
    # The holiday is on Ann's branch calendar (precondition read from the run's own day ledger).
    _, detail = lines_of(w, run_id, "Ann")
    ledger = {d["date"]: d for d in detail["ledger"]["days"]}
    assert ledger[holiday_day.isoformat()]["holiday"], ledger[holiday_day.isoformat()]
    assert ledger[we[0].isoformat()]["weekly_off"] is True
    components = ("HOLIDAY_PREMIUM", "WEEKLY_OFF_PREMIUM", "OT_HOLIDAY", "OT_WEEKLY_OFF")

    def extra():
        lines, _ = lines_of(w, run_id, "Ann")
        return {c: lines[c] for c in components if c in lines}

    # OFF / OFF / OFF: nothing extra.
    assert extra() == {}
    # Premiums ON, overtime OFF: every hour at the premium (125.00 x 1.0 x 8 h; 125.00 x 0.5 x 10 h).
    switch(w, premium, True)
    switch(w, weekly, True)
    recalculated(w, run_id)
    assert extra() == {"HOLIDAY_PREMIUM": D("1000.00"), "WEEKLY_OFF_PREMIUM": D("625.00")}, describe(w, run_id, "Ann")
    # Premiums OFF, overtime ON: every hour to the overtime bucket (8 h x 125.00 x 3.0; 10 h x 125.00 x 2.0).
    switch(w, premium, False)
    switch(w, weekly, False)
    switch(w, ot, True)
    recalculated(w, run_id)
    assert extra() == {"OT_HOLIDAY": D("3000.00"), "OT_WEEKLY_OFF": D("2500.00")}, describe(w, run_id, "Ann")
    # Both ON: premium up to the 8 standard hours, overtime beyond (no hour paid twice):
    # holiday 8 h x 125.00 x 1.0 = 1,000.00 (nothing beyond); weekly off 8 h x 125.00 x 0.5 = 500.00 and
    # 2 h x 125.00 x 2.0 = 500.00 overtime.
    switch(w, premium, True)
    switch(w, weekly, True)
    recalculated(w, run_id)
    assert extra() == {"HOLIDAY_PREMIUM": D("1000.00"), "WEEKLY_OFF_PREMIUM": D("500.00"),
                       "OT_WEEKLY_OFF": D("500.00")}, describe(w, run_id, "Ann")
    # Another policy: weekly-off multiplier 1.5 -> 8 h x 125.00 x 1.5 = 1,500.00.
    activate_policy(w["admin"], "HOLIDAY_PAY_POLICY", {"holiday_work_multiplier": "1.0",
                                                        "weekly_off_work_multiplier": "1.5"})
    recalculated(w, run_id)
    assert extra() == {"HOLIDAY_PREMIUM": D("1000.00"), "WEEKLY_OFF_PREMIUM": D("1500.00"),
                       "OT_WEEKLY_OFF": D("500.00")}
    # Pay-group override: weekly-off pay OFF for PG-A -> its 10 h go to overtime; PG-B keeps the premium.
    switch(w, weekly, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert extra() == {"HOLIDAY_PREMIUM": D("1000.00"), "OT_WEEKLY_OFF": D("2500.00")}
    cal_run = fresh_run(w, "PG-B")
    cal = lines_of(w, cal_run, "Cal")[0]
    assert cal.get("WEEKLY_OFF_PREMIUM") == D("1500.00") and "OT_WEEKLY_OFF" not in cal, cal
    assert_bystander_unchanged(settings_world, (premium, False), (weekly, False))


# ---------------------------------------------------------------------------
# Shift / night allowance (3.1 §5.8)
# ---------------------------------------------------------------------------
def test_shift_allowance_cycle(settings_world):
    w = settings_world["alpha"]
    wd = past_weekdays(w)
    attendance(w, "Ann", wd[3], start=22, end=6, next_day=True)   # 8 h inside the 22:00-06:00 window
    attendance(w, "Cal", wd[3], start=22, end=6, next_day=True)
    key = "payroll.shift_allowance.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is False
    money = lambda a: {"amount": a, "currency": "GBP"}  # noqa: E731
    activate_policy(w["admin"], "SHIFT_ALLOWANCE_POLICY", {"allowance_type": "PER_SHIFT_AMOUNT",
                                                            "amount": money("20.00")})
    switch(w, key, True)
    run_id = fresh_run(w, "PG-A")
    assert amount(w, run_id, "Ann", "SHIFT_ALLOWANCE") == D("20.00"), describe(w, run_id, "Ann")   # 1 shift x 20
    assert amount(w, run_id, "Ben", "SHIFT_ALLOWANCE") is None
    switch(w, key, False)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "SHIFT_ALLOWANCE") is None
    activate_policy(w["admin"], "SHIFT_ALLOWANCE_POLICY", {"allowance_type": "PER_SHIFT_AMOUNT",
                                                            "amount": money("35.00")})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "SHIFT_ALLOWANCE") == D("35.00")
    activate_policy(w["admin"], "SHIFT_ALLOWANCE_POLICY", {"allowance_type": "FIXED_MONTHLY_IF_ANY",
                                                            "amount": money("100.00")},
                    name="Ann monthly night allowance", criteria={"employee_ids": [w["emp"]["Ann"]]}, priority=10)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "SHIFT_ALLOWANCE") == D("100.00")
    cal_run = fresh_run(w, "PG-B")
    assert amount(w, cal_run, "Cal", "SHIFT_ALLOWANCE") == D("35.00")   # Cal keeps the catch-all policy
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "SHIFT_ALLOWANCE") is None
    recalculated(w, cal_run, expect_stale=None)
    assert amount(w, cal_run, "Cal", "SHIFT_ALLOWANCE") == D("35.00")
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Late arrival / early exit penalties (3.1 §5.7; lawful only where no pack forbids them: see the ZZ test in
# test_settings_inheritance.py)
# ---------------------------------------------------------------------------
def test_late_penalty_cycle(settings_world):
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    wd = past_weekdays(w)
    for day in wd[2:5]:
        attendance(w, "Ann", day, "late", 10, 18)
        attendance(w, "Cal", day, "late", 10, 18)
    key = "payroll.late_penalty.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is False
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, "payroll.attendance.enabled", True)                  # V-01: penalties need attendance
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {"free_late_marks": 0, "late_marks_per_unit": 1,
                                                         "unit_days": "0.5", "cap_days_per_period": "2.0"})
    switch(w, key, True)
    run_id = fresh_run(w, "PG-A")
    # 3 chargeable marks, 1 mark per unit, 0.5 day each -> 1.5 days of penalty LOP (cap 2.0 not reached).
    assert basic(w, run_id, "Ann") == month - D("1500.00"), describe(w, run_id, "Ann")
    switch(w, key, False)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month                        # late marks never change pay when OFF
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {"free_late_marks": 2, "late_marks_per_unit": 1,
                                                         "unit_days": "0.5", "cap_days_per_period": "2.0"})
    switch(w, key, True)
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("500.00")          # 3 - 2 free = 1 chargeable -> 0.5 day
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    cal_run = fresh_run(w, "PG-B")
    assert basic(w, cal_run, "Cal") == month - D("500.00")
    assert_bystander_unchanged(settings_world, (key, False))


def local_at(w, day, hour, minute=0):
    """An instant at ``hour:minute`` on ``day`` in the company's time zone (shift windows are local)."""
    import pytz

    from tests.payroll.verification.test_settings_inheritance import sql

    zone = pytz.timezone(sql("SELECT timezone FROM companies WHERE id = :c", c=w["cid"])[0][0] or "UTC")
    return zone.localize(datetime.combine(day, time(hour, minute))).isoformat()


def test_early_exit_penalty_cycle(settings_world):
    """Early exits (3.4 §11) are chargeable only with ``count_early_exit`` and beyond the policy's grace. Ann and
    Cal work a 09:00-17:00 shift and leave at 15:00 (120 minutes early) on three past working days."""
    w = settings_world["alpha"]
    month = monthly_basic(w["period"])
    wd = past_weekdays(w)
    shift = ok(w["admin"].post("/api/v1/shifts", json={"name": "Day shift", "start_time": "09:00",
                                                       "end_time": "17:00"}))["id"]
    for person in ("Ann", "Cal"):
        ok(w["admin"].put(f"/api/v1/employees/{w['emp'][person]}", json={"shift_id": shift}))
        for day in wd[2:5]:
            ok(w["admin"].post("/api/v1/attendance/manual", json={
                "employee_id": w["emp"][person], "date": day.isoformat(), "status": "present",
                "check_in_time": local_at(w, day, 9), "check_out_time": local_at(w, day, 15)}))
    key = "payroll.late_penalty.enabled"
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, "payroll.attendance.enabled", True)
    base = {"free_late_marks": 0, "late_marks_per_unit": 1, "unit_days": "0.5", "cap_days_per_period": "2.0",
            "early_exit_grace_minutes": 15}
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {**base, "count_early_exit": False})
    switch(w, key, True)
    run_id = fresh_run(w, "PG-A")
    # Early exits do not count under this policy: no penalty.
    assert basic(w, run_id, "Ann") == month, describe(w, run_id, "Ann")
    # They count: 3 marks x 0.5 day = 1.5 days (below the 2.0 cap) -> 1,500.00.
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {**base, "count_early_exit": True})
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month - D("1500.00"), describe(w, run_id, "Ann")
    # A grace of 180 minutes covers the 120-minute early exits: no mark, no penalty.
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {**base, "count_early_exit": True,
                                                         "early_exit_grace_minutes": 180})
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    # Back to a 15-minute grace; switch OFF for PG-A only: Ann pays nothing, Cal (PG-B, tenant ON) 1,500.00.
    activate_policy(w["admin"], "LATE_PENALTY_POLICY", {**base, "count_early_exit": True})
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert basic(w, run_id, "Ann") == month
    cal_run = fresh_run(w, "PG-B")
    assert basic(w, cal_run, "Cal") == month - D("1500.00"), describe(w, cal_run, "Cal")
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Task / milestone / piece-rate pay (3.1 §5.6)
# ---------------------------------------------------------------------------
def approved_task(w, person, quantity, rate="50.00", task_type="INSTALL"):
    task = ok(w["admin"].post("/api/v1/tasks", json={
        "title": f"{task_type} for {person}", "task_type": task_type, "quantity": quantity, "unit": "unit",
        "rate": {"amount": rate, "currency": "GBP"}, "assignees": [{"employee_id": w["emp"][person]}],
        "completed_on": today_of(w["admin"]).isoformat()}, headers=idem()), 201)
    ok(w["admin"].post(f"/api/v1/tasks/{task['id']}/submit", headers=idem()))
    ok(w["manager"].post(f"/api/v1/tasks/{task['id']}/approve", json={}, headers=idem()))
    return task["id"]


def test_task_pay_cycle(settings_world):
    w = settings_world["alpha"]
    key = "payroll.task_pay.enabled"
    found = resolve(w["admin"], key, employee_id=w["emp"]["Cal"], on=w["end"])
    assert found["value"] is True                                  # starter: ON (approved tasks only, I-13)
    activate_policy(w["admin"], "TASK_PAY_POLICY", {"requires_evidence": False, "rate_source": "TASK",
                                                     "attribution": "APPROVAL_DATE"})
    # The starter rows start on the signup day; before it the registry default (tasks module OFF) applies, so the
    # module is switched on from the period start first (V-08 is checked on the change's effective date).
    switch(w, "tasks.enabled", True)
    switch(w, key, True)
    approved_task(w, "Cal", "4")                                   # piece rate: 4 units x 50.00
    approved_task(w, "Dee", "2")                                   # 2 x 50.00
    run_id = fresh_run(w, "PG-B")
    assert amount(w, run_id, "Cal", "TASK_EARNING") == D("200.00"), describe(w, run_id, "Cal")
    switch(w, key, False)
    recalculated(w, run_id)
    assert amount(w, run_id, "Cal", "TASK_EARNING") is None
    activate_policy(w["admin"], "TASK_PAY_POLICY", {"requires_evidence": False, "rate_source": "TASK",
                                                     "cap_per_period": {"amount": "150.00", "currency": "GBP"},
                                                     "cap_action": "TRUNCATE"})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Cal", "TASK_EARNING") == D("150.00")   # 200.00 truncated at the 150.00 cap
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-B"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Cal", "TASK_EARNING") is None
    dee_run = fresh_run(w, "PG-C")
    assert amount(w, dee_run, "Dee", "TASK_EARNING") == D("100.00")   # below the cap; PG-C follows the tenant
    assert_bystander_unchanged(settings_world, (key, True))


# ---------------------------------------------------------------------------
# Bonuses and commissions as wage inputs (3.1 §5.9)
# ---------------------------------------------------------------------------
def wage_input(w, person, component, value, approve=True):
    created = ok(w["admin"].post(f"{PAY}/wage-inputs", json={
        "employee_id": w["emp"][person], "component_code": component, "period_start": w["start"].isoformat(),
        "period_end": w["end"].isoformat(), "amount": {"amount": value, "currency": "GBP"},
        "reason": f"{component.title()} for the period"}, headers=idem()), 201)
    item = created["items"][0]
    if approve and item["status"] == "PENDING_APPROVAL":
        approve_request(w, item["approval_request_id"], deciders=("approver", "second_admin"))
    return item


def test_bonus_and_commission_wage_input_cycle(settings_world):
    w = settings_world["alpha"]
    key = "payroll.wage_inputs.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ben"], on=w["end"])["value"] is True
    wage_input(w, "Ben", "BONUS", "500.00")
    wage_input(w, "Ben", "COMMISSION", "250.00")
    wage_input(w, "Cal", "BONUS", "300.00")
    month = monthly_basic(w["period"])
    run_id = fresh_run(w, "PG-A")
    lines, detail = lines_of(w, run_id, "Ben")
    assert (lines.get("BONUS"), lines.get("COMMISSION")) == (D("500.00"), D("250.00")), describe(w, run_id, "Ben")
    assert D(detail["totals"]["gross"]["amount"]) == month + D("750.00")
    switch(w, key, False)
    recalculated(w, run_id)
    lines, _ = lines_of(w, run_id, "Ben")
    assert "BONUS" not in lines and "COMMISSION" not in lines
    assert findings_of(w, run_id, "WAGE_INPUTS_DISABLED"), open_findings(w["admin"], run_id)
    r = w["admin"].post(f"{PAY}/wage-inputs", json={
        "employee_id": w["emp"]["Ben"], "component_code": "BONUS", "period_start": w["start"].isoformat(),
        "period_end": w["end"].isoformat(), "amount": {"amount": "10.00", "currency": "GBP"}}, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "WAGE_INPUTS_DISABLED", r.text
    # Re-enable with another approval threshold: inputs up to 1,000.00 GBP need no approval.
    switch(w, key, True)
    switch(w, "payroll.wage_inputs.require_approval_above", {"amount": "1000.00", "currency": "GBP"})
    extra = wage_input(w, "Ben", "BONUS", "200.00", approve=False)
    assert extra["status"] == "APPROVED", extra
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "BONUS") == D("700.00")          # 500.00 + 200.00
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Ben", "BONUS") is None
    cal_run = fresh_run(w, "PG-B")
    assert amount(w, cal_run, "Cal", "BONUS") == D("300.00")
    assert_bystander_unchanged(settings_world, (key, True))


def test_wage_input_settings_follow_the_period_grain(settings_world):
    """Regression (EC-CFG-02): PERIOD-grain wage-input settings are read on the period end, and a component
    that starts during the period (a tenant provisioned mid-period) accepts inputs for that period."""
    w = settings_world["alpha"]
    end_minus = w["end"] - timedelta(days=1)
    put_setting(w["admin"], "payroll.wage_inputs.require_approval_above", {"amount": "1000.00", "currency": "GBP"},
                effective_from=end_minus, reason=REASON if end_minus < today_of(w["admin"]) else None)
    item = wage_input(w, "Ben", "BONUS", "200.00", approve=False)
    assert item["status"] == "APPROVED", item                         # the value on the period end applies
    put_setting(w["admin"], "payroll.wage_inputs.enabled", False, effective_from=end_minus,
                reason=REASON if end_minus < today_of(w["admin"]) else None)
    r = w["admin"].post(f"{PAY}/wage-inputs", json={
        "employee_id": w["emp"]["Cal"], "component_code": "BONUS", "period_start": w["start"].isoformat(),
        "period_end": w["end"].isoformat(), "amount": {"amount": "10.00", "currency": "GBP"}}, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "WAGE_INPUTS_DISABLED", r.text


# ---------------------------------------------------------------------------
# Incentive plans (3.1 §5.9 INCENTIVE_PLAN, decision I-22): approved achievement measures -> INCENTIVE line
# ---------------------------------------------------------------------------
def measure(w, person, value, *, approve=True, component="INCENTIVE", expect=201):
    """An achievement measure for the period (a wage input with source INCENTIVE_PLAN), approved by default."""
    r = w["admin"].post(f"{PAY}/wage-inputs", json={
        "employee_id": w["emp"][person], "component_code": component, "period_start": w["start"].isoformat(),
        "period_end": w["end"].isoformat(), "measure": value, "reason": "Sales achievement"}, headers=idem())
    if expect != 201:
        assert r.status_code == expect, r.text
        return r.json()
    item = ok(r, 201)["items"][0]
    assert item["status"] == "PENDING_APPROVAL", item                    # a measure is always approved by a person
    if approve:
        approve_request(w, item["approval_request_id"], deciders=("approver", "second_admin"))
    return item


INCENTIVE_TIERED = "IF(inc.achievement_pct >= 100, inc.measure * 4, inc.measure * 2)"


def test_incentive_plan_cycle(settings_world):
    w = settings_world["alpha"]
    key = "payroll.incentives.enabled"
    gbp = lambda a: {"amount": a, "currency": "GBP"}  # noqa: E731
    # (1) registry default OFF; no measure can be entered while it is OFF.
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is False
    assert measure(w, "Ann", "60", expect=409)["code"] == "INCENTIVES_DISABLED"
    # (2)-(3) ON with a plan: own targets Ann 100, Ben 200, Cal 100; cap 600.00 GBP per payout. The pay calendar has
    # no tax-year rule, so the MONTHLY window is the calendar month = this period (the run pays it).
    targets = [{"employee_id": w["emp"][p], "target": t} for p, t in (("Ann", "100"), ("Ben", "200"), ("Cal", "100"))]
    activate_policy(w["admin"], "INCENTIVE_PLAN", {"formula": INCENTIVE_TIERED, "targets": targets,
                                                    "cap": gbp("600.00")})
    switch(w, key, True)
    # (4) approved measures: Ann 60 + 50, Ben 150, Cal 300.
    for person, value in (("Ann", "60"), ("Ann", "50"), ("Ben", "150"), ("Cal", "300")):
        measure(w, person, value)
    month = monthly_basic(w["period"])
    run_id = fresh_run(w, "PG-A")
    # (5)-(6) Ann: 110 against 100 = 110 % >= 100 -> 110 x 4 = 440.00. Ben: 150 against 200 = 75 % -> 150 x 2 = 300.00.
    lines, detail = lines_of(w, run_id, "Ann")
    assert lines == {"BASIC": month, "INCENTIVE": D("440.00")}, describe(w, run_id, "Ann")
    assert D(detail["totals"]["gross"]["amount"]) == month + D("440.00")
    assert D(detail["totals"]["net"]["amount"]) == month + D("440.00")
    assert amount(w, run_id, "Ben", "INCENTIVE") == D("300.00")
    # (7)-(9) OFF: the recalculated draft pays no incentive and says why; no new measure can be entered.
    switch(w, key, False)
    recalculated(w, run_id)
    assert lines_of(w, run_id, "Ann")[0] == {"BASIC": month}
    assert findings_of(w, run_id, "INCENTIVES_DISABLED"), open_findings(w["admin"], run_id)
    assert measure(w, "Ann", "5", expect=409)["code"] == "INCENTIVES_DISABLED"
    # (10)-(11) ON with another plan: 3 per unit, cap 400.00 GBP. Ann 110 x 3 = 330.00; Ben 150 x 3 = 450.00 -> 400.00.
    activate_policy(w["admin"], "INCENTIVE_PLAN", {"formula": "inc.measure * 3", "targets": targets,
                                                    "cap": gbp("400.00")})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "INCENTIVE") == D("330.00"), describe(w, run_id, "Ann")
    assert amount(w, run_id, "Ben", "INCENTIVE") == D("400.00")
    # (12)-(13) pay group PG-A OFF: Ann and Ben get nothing; Cal (PG-B follows the tenant) 300 x 3 = 900.00 -> 400.00.
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-A"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Ann", "INCENTIVE") is None and amount(w, run_id, "Ben", "INCENTIVE") is None
    cal_run = fresh_run(w, "PG-B")
    assert amount(w, cal_run, "Cal", "INCENTIVE") == D("400.00"), describe(w, cal_run, "Cal")
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Reimbursements (3.1 §5.10)
# ---------------------------------------------------------------------------
GENERAL = {"code": "GENERAL", "name": "General expense"}


def claim(w, person, claim_type, value, day):
    created = ok(w["admin"].post(f"{PAY}/reimbursement-claims", json={
        "employee_id": w["emp"][person], "claim_type": claim_type, "expense_date": day.isoformat(),
        "amount": {"amount": value, "currency": "GBP"}, "merchant": "Rail operator"}, headers=idem()), 201)
    approve_request(w, created["approval_request_id"], deciders=("approver", "second_admin", "manager"))
    return created["id"]


def test_reimbursement_cycle(settings_world):
    w = settings_world["alpha"]
    wd = past_weekdays(w)
    key = "payroll.reimbursements.enabled"
    month = monthly_basic(w["period"])
    activate_policy(w["admin"], "REIMBURSEMENT_POLICY", {"claim_types": [GENERAL]})
    switch(w, key, True)
    claim(w, "Cal", "GENERAL", "120.00", wd[0])
    claim(w, "Dee", "GENERAL", "45.50", wd[0])
    run_id = fresh_run(w, "PG-B")
    lines, detail = lines_of(w, run_id, "Cal")
    assert lines.get("REIMBURSEMENT") == D("120.00"), describe(w, run_id, "Cal")
    # Net = cash earnings - deductions + cash reimbursements = month + 120.00 (a reimbursement is not wages).
    assert D(detail["totals"]["net"]["amount"]) == month + D("120.00")
    assert D(detail["totals"]["gross"]["amount"]) == month
    switch(w, key, False)
    recalculated(w, run_id)
    lines, detail = lines_of(w, run_id, "Cal")
    assert "REIMBURSEMENT" not in lines and D(detail["totals"]["net"]["amount"]) == month
    assert findings_of(w, run_id, "REIMBURSEMENTS_DISABLED")
    # Another policy: a TRAVEL claim type is added; Cal's travel claim is paid with the general one.
    activate_policy(w["admin"], "REIMBURSEMENT_POLICY", {"claim_types": [GENERAL, {"code": "TRAVEL",
                                                                                     "name": "Travel"}]})
    switch(w, key, True)
    claim(w, "Cal", "TRAVEL", "80.00", wd[1])
    recalculated(w, run_id)
    assert amount(w, run_id, "Cal", "REIMBURSEMENT") == D("200.00")     # 120.00 + 80.00
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-B"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Cal", "REIMBURSEMENT") is None
    dee_run = fresh_run(w, "PG-C")
    assert amount(w, dee_run, "Dee", "REIMBURSEMENT") == D("45.50")
    # Payout route SEPARATE_PAYMENT for VA2 (Dee): the claim leaves the payroll run (paid in its own batch).
    route = "payroll.reimbursements.payout_route"
    assert resolve(w["admin"], route, employee_id=w["emp"]["Dee"], on=w["end"])["value"] == "WITH_PAYROLL"
    switch(w, route, "SEPARATE_PAYMENT", "LEGAL_ENTITY", w["entities"]["VA2"])
    recalculated(w, dee_run)
    lines, detail = lines_of(w, dee_run, "Dee")
    assert "REIMBURSEMENT" not in lines and D(detail["totals"]["net"]["amount"]) == month, describe(w, dee_run, "Dee")
    assert_bystander_unchanged(settings_world, (key, True), (route, "WITH_PAYROLL"))


# ---------------------------------------------------------------------------
# Loan recovery and salary advances (3.1 §5.11)
# ---------------------------------------------------------------------------
def disbursed_loan(w, person, kind, value, tenure=None):
    body = {"employee_id": w["emp"][person], "kind": kind, "amount": {"amount": value, "currency": "GBP"},
            "purpose": "Verification"}
    if tenure is not None:
        body["tenure"] = tenure
    loan = ok(w["admin"].post(f"{PAY}/loans", json=body, headers=idem()), 201)
    approve_request(w, loan["approval_request_id"], deciders=("approver", "second_admin"))
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import loans

    db = SessionLocal()
    try:
        bind_tenant(db, w["cid"])
        # Setup shortcut (module docstring): the payment-evidence hook records the disbursement.
        loans.record_disbursement(db, None, w["cid"], loan["id"], paid_on=w["start"],
                                  reference=f"VERIFY-{loan['id']}")
        db.commit()
    finally:
        db.close()
    return loan["id"]


def test_loan_recovery_cycle(settings_world):
    w = settings_world["alpha"]
    key = "payroll.loans.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Dee"], on=w["end"])["value"] is False   # starter OFF
    switch(w, "payroll.deductions.min_net_pay_percent", "50")      # required before automated recoveries (G-23)
    activate_policy(w["admin"], "LOAN_POLICY", {"max_amount_formula": "3 * MONTHLY_GROSS", "interest_rate_percent": "0",
                                                 "interest_method": "FLAT", "max_tenure_months": 24,
                                                 "max_active_loans": 2})
    switch(w, key, True)
    disbursed_loan(w, "Dee", "LOAN", "1200.00", tenure=12)        # 1,200.00 / 12 = 100.00 a month, no interest
    disbursed_loan(w, "Ben", "LOAN", "2400.00", tenure=12)        # 200.00 a month
    month = monthly_basic(w["period"])
    run_id = fresh_run(w, "PG-C")
    lines, detail = lines_of(w, run_id, "Dee")
    assert lines.get("LOAN_RECOVERY") == D("100.00"), describe(w, run_id, "Dee")
    assert D(detail["totals"]["net"]["amount"]) == month - D("100.00")
    switch(w, key, False)
    recalculated(w, run_id)
    lines, detail = lines_of(w, run_id, "Dee")
    assert "LOAN_RECOVERY" not in lines and D(detail["totals"]["net"]["amount"]) == month
    # Re-enabled with another loan policy (2 % flat interest) for new loans; Dee's schedule is fixed at approval.
    activate_policy(w["admin"], "LOAN_POLICY", {"max_amount_formula": "3 * MONTHLY_GROSS", "interest_rate_percent": "2",
                                                 "interest_method": "FLAT", "max_tenure_months": 24,
                                                 "max_active_loans": 2})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Dee", "LOAN_RECOVERY") == D("100.00")
    # Entity override: loans OFF for VA2 (Dee) only; Ben (VA1) still repays 200.00.
    switch(w, key, False, "LEGAL_ENTITY", w["entities"]["VA2"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Dee", "LOAN_RECOVERY") is None
    ben_run = fresh_run(w, "PG-A")
    assert amount(w, ben_run, "Ben", "LOAN_RECOVERY") == D("200.00"), describe(w, ben_run, "Ben")
    assert_bystander_unchanged(settings_world, (key, False))


def test_salary_advance_switch_gates_applications(settings_world):
    """Advances: the switch is checked when an advance is applied for (LOANS_DISABLED when OFF, at TENANT or at the
    employee's legal entity). With the switch ON the application reaches the policy check, which needs the monthly
    net pay of a finalized regular run whose period has ended (``snapshot.pay_figures``: period_end <= today);
    the harness's tenants are provisioned in the current period, so the recovery itself is listed in
    BLOCKED_FEATURES with that reason."""
    w = settings_world["alpha"]
    key = "payroll.advances.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Dee"], on=w["end"])["value"] is False   # starter OFF
    body = {"employee_id": w["emp"]["Dee"], "kind": "ADVANCE", "amount": {"amount": "600.00", "currency": "GBP"},
            "purpose": "Verification"}
    r = w["admin"].post(f"{PAY}/loans", json=body, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "LOANS_DISABLED", r.text
    put_setting(w["admin"], "payroll.deductions.min_net_pay_percent", "50")
    activate_policy(w["admin"], "ADVANCE_POLICY", {"recover_in_runs": 1, "max_per_year": 3})
    put_setting(w["admin"], key, True)
    r = w["admin"].post(f"{PAY}/loans", json=body, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "ADVANCE_NET_UNKNOWN", r.text   # fails closed, never guessed
    put_setting(w["admin"], key, False, "LEGAL_ENTITY", w["entities"]["VA2"])
    r = w["admin"].post(f"{PAY}/loans", json=body, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "LOANS_DISABLED", r.text        # entity override wins
    ben = {**body, "employee_id": w["emp"]["Ben"]}
    r = w["admin"].post(f"{PAY}/loans", json=ben, headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "ADVANCE_NET_UNKNOWN", r.text   # VA1 follows the tenant: ON
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Bank verification requirement and payment exclusion of unverified accounts (3.1 §5.16)
# ---------------------------------------------------------------------------
def test_bank_verification_requirement_and_unverified_payout_hold(settings_world):
    from tests.payroll.test_payment_obligations import add_bank_account

    w = settings_world["alpha"]
    # Setup shortcut (module docstring): an approved account past cooling-off, not yet verified.
    add_bank_account(w["cid"], w["emp"]["Ann"], "50000012345678", verified=False, holder="Ann Verify",
                     currency="GBP")
    require, exclude = "payroll.bank.require_verification", "payroll.bank.exclude_unverified_from_payout"
    for k in (require, exclude):
        assert resolve(w["admin"], k, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is True
    run_id = fresh_run(w, "PG-A")
    ann = w["emp"]["Ann"]
    assert [f for f in findings_of(w, run_id, "UNVERIFIED_BANK") if f["employee_id"] == ann]
    assert [f for f in findings_of(w, run_id, "UNVERIFIED_ACCOUNT_ON_HOLD") if f["employee_id"] == ann], \
        describe(w, run_id, "Ann")
    put_setting(w["admin"], exclude, False, effective_from=w["start"], reason=REASON)
    recalculated(w, run_id)
    assert not findings_of(w, run_id, "UNVERIFIED_ACCOUNT_ON_HOLD")
    put_setting(w["admin"], require, False, effective_from=w["start"], reason=REASON)
    # Pre-checks run before the first calculation only: cancel the draft and check a new run.
    ok(w["admin"].post(f"{PAY}/runs/{run_id}/cancel", json={"reason": "Re-run the pre-checks"}, headers=idem()))
    run_id = fresh_run(w, "PG-A")
    assert not findings_of(w, run_id, "UNVERIFIED_BANK")
    put_setting(w["admin"], exclude, True, "LEGAL_ENTITY", w["entities"]["VA1"], effective_from=w["start"],
                reason=REASON)
    recalculated(w, run_id)
    assert [f for f in findings_of(w, run_id, "UNVERIFIED_ACCOUNT_ON_HOLD") if f["employee_id"] == ann]
    assert_bystander_unchanged(settings_world, (require, True), (exclude, True))


# ---------------------------------------------------------------------------
# Payment dual control (3.7 §8.4): the preparer of a payment batch may not approve it while ON
# ---------------------------------------------------------------------------
DC_REASON = "Verification: payment dual control cycle"


def test_payment_dual_control_cycle(app):
    """The run is moved to FINALIZED by the payment tests' helper (``finalized_run``: no amounts are calculated
    here, the switch governs who may approve a batch). Batches hold one payment each (batch_max_items = 1 set
    through the settings API) so each step works on a fresh batch. Steps (3) and (10) do not apply: the control
    has no policy."""
    from tests.helpers import create_employee
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_payment_obligations import finalized_run, payments_world

    key = "payroll.approval.payment_dual_control"
    # The finalized run covers today, so a change effective today falls in a finalized period: the product
    # requires a reason for it (REASON_REQUIRED otherwise), which is given.
    batches = f"{PAY}/payment-batches"
    world = payments_world(app, "Dual Control Pay", employees=("Emma", "Ivan", "Zara"))
    bystander = payments_world(app, "Bystander Pay", employees=("Emma",))
    admin, pete, alma, le = world["admin"], world["pete"], world["alma"], world["le"]
    # (1) default ON (registry and starter value), read at the entity.
    found = resolve(admin, key, legal_entity_id=le)
    assert found["value"] is True, found
    put_setting(admin, "payroll.payments.batch_max_items", 1, "LEGAL_ENTITY", le, reason=DC_REASON)
    run_id = finalized_run(world, {"Emma": "100.00", "Ivan": "200.00", "Zara": "300.00"})
    first, second, third = ok(pete.post(batches, json={"run_id": run_id}), 201)["batches"]
    # (5)-(6) ON: Pete prepared the batch and may not approve it.
    r = pete.post(f"{batches}/{first['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "DUAL_CONTROL", r.text
    # The starter profile LOCKS the control at TENANT (STARTER_V1 locks): no entity may switch it off while the
    # lock holds, and the refusal names the lock.
    refused = put_setting(admin, key, False, "LEGAL_ENTITY", le, reason=DC_REASON, expect=400)
    assert refused["code"] == "SETTING_LOCKED", refused
    assert resolve(admin, key, legal_entity_id=le)["value"] is True
    # Maker-checker on the switch itself (the key requires approval when change approval is ON): lifting the
    # tenant lock and turning the control OFF each stay pending until another administrator approves them, and
    # the control holds meanwhile.
    checker = login_as(app, admin, create_employee(admin, "Cleo", "Checker"), ["company_admin", "payroll_manager"])
    put_setting(admin, "settings.change_approval.enabled", True, reason=DC_REASON)
    unlock = put_setting(admin, key, True, locked=False, reason=DC_REASON)["changes"][0]
    assert unlock["status"] == "PENDING_APPROVAL", unlock
    assert put_setting(admin, key, False, "LEGAL_ENTITY", le, reason=DC_REASON,
                       expect=400)["code"] == "SETTING_LOCKED"            # the lock holds while the unlock is pending
    ok(checker.post(f"/api/v1/payroll/settings/change-requests/{unlock['change_request_id']}/approve",
                    json={"comment": "Entities decide"}))
    pending = put_setting(admin, key, False, "LEGAL_ENTITY", le, reason=DC_REASON)["changes"][0]
    assert pending["status"] == "PENDING_APPROVAL", pending
    assert pete.post(f"{batches}/{first['id']}/approve").status_code == 403
    ok(checker.post(f"/api/v1/payroll/settings/change-requests/{pending['change_request_id']}/approve",
                    json={"comment": "Small team"}))
    # (7)-(9) OFF at the entity (overrides the TENANT ON): the preparer may approve his own batch.
    found = resolve(admin, key, legal_entity_id=le)
    assert (found["value"], found["level"]) == (False, "LEGAL_ENTITY"), found
    assert ok(pete.post(f"{batches}/{first['id']}/approve"))["status"] == "APPROVED"
    # (10)-(11) ON again (entity value back to ON, approved by the checker): the second batch needs Alma.
    pending = put_setting(admin, key, True, "LEGAL_ENTITY", le, reason=DC_REASON)["changes"][0]
    ok(checker.post(f"/api/v1/payroll/settings/change-requests/{pending['change_request_id']}/approve", json={}))
    r = pete.post(f"{batches}/{second['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "DUAL_CONTROL", r.text
    assert ok(alma.post(f"{batches}/{second['id']}/approve"))["status"] == "APPROVED"
    # (12) TENANT OFF does not reach the entity while the entity says ON (the lower level wins).
    pending = put_setting(admin, key, False, reason=DC_REASON)["changes"][0]
    ok(checker.post(f"/api/v1/payroll/settings/change-requests/{pending['change_request_id']}/approve", json={}))
    assert resolve(admin, key, legal_entity_id=le)["level"] == "LEGAL_ENTITY"
    r = pete.post(f"{batches}/{third['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "DUAL_CONTROL", r.text
    # (13) the bystander tenant still has the control ON: its preparer is refused.
    assert resolve(bystander["admin"], key, legal_entity_id=bystander["le"])["value"] is True
    other_run = finalized_run(bystander, {"Emma": "100.00"})
    [other] = ok(bystander["pete"].post(batches, json={"run_id": other_run}), 201)["batches"]
    r = bystander["pete"].post(f"{batches}/{other['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "DUAL_CONTROL", r.text


# ---------------------------------------------------------------------------
# Holiday pay for hourly workers (3.1 §5.4, I-16: payroll.holiday_pay.non_salaried.enabled)
# ---------------------------------------------------------------------------
def hourly_worker(w, name, group_code, branch_code, entity_code, rate="20"):
    """An hourly worker on structure HOURLY (HOURLY_PAY = rate x att.payable_hours), through the real APIs."""
    from tests.helpers import create_employee
    from tests.payroll.test_compensation import assign_and_approve
    from tests.payroll.verification.conftest import place

    admin = w["admin"]
    if "hourly_version" not in w:
        ok(admin.post(f"{PAY}/components", json={
            "code": "HOURLY_PAY", "kind": "EARNING", "category": "BASE", "name": "Hourly pay",
            "calc_method": "FIXED", "tags": ["PART_OF_GROSS"], "proration": "NONE",
            "effective_from": "2020-01-01"}), 201)
        created = ok(admin.post(f"{PAY}/salary-structures", json={
            "code": "HOURLY", "name": "Hourly", "type": "HOURLY",
            "lines": [{"component": "HOURLY_PAY", "method": "RATE_X_UNITS"}],
            "test_range": {"min": "10", "max": "100"}}), 201)
        published = ok(admin.post(f"{PAY}/salary-structures/{created['id']}/publish", json={"version": 1}))
        w["hourly_version"] = next(v["id"] for v in published["versions"] if v["status"] == "ACTIVE")
    employee = create_employee(admin, name, "Hourly", manager_id=w["manager_id"], hire_date="2024-01-15")
    place(admin, employee["id"], w["entities"][entity_code], w["branches"][branch_code], w["groups"][group_code])
    assign_and_approve(admin, w["approver"], employee["id"], w["hourly_version"], rate, effective_from=w["start"],
                       currency="GBP")
    w["emp"][name] = employee["id"]
    return employee["id"]


def test_holiday_pay_for_hourly_workers_cycle(settings_world):
    w = settings_world["alpha"]
    wd = past_weekdays(w)
    holiday_day = wd[5]
    ok(w["admin"].post("/api/v1/holidays", json={"name": "Verification Day", "date": holiday_day.isoformat()}))
    hourly_worker(w, "Hal", "PG-B", "MAN", "VA1")
    key = "payroll.holiday_pay.non_salaried.enabled"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Hal"], on=w["end"])["value"] is False
    wk = weekday_count(w)
    # att.payable_hours exists only with attendance ON (an hourly line fails closed without it): unmarked working
    # days are presumed present at the 8 standard hours.
    activate_policy(w["admin"], "ATTENDANCE_PAY_POLICY", {"unmarked_day": "PRESENT"})
    switch(w, "payroll.attendance.enabled", True)
    run_id = fresh_run(w, "PG-B")
    _, detail = lines_of(w, run_id, "Hal")
    assert {d["date"]: d for d in detail["ledger"]["days"]}[holiday_day.isoformat()]["holiday"], "holiday precondition"
    # Every working day presumed present at 8 h; the holiday is not a working day and is unpaid for an hourly
    # worker while the switch is OFF: 20.00 x 8 x (W - 1).
    assert amount(w, run_id, "Hal", "HOURLY_PAY") == D(20) * 8 * (wk - 1), describe(w, run_id, "Hal")
    activate_policy(w["admin"], "HOLIDAY_PAY_POLICY", {"non_salaried_holiday_pay": "STANDARD_HOURS"})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Hal", "HOURLY_PAY") == D(20) * 8 * wk     # the holiday's 8 standard hours paid
    switch(w, key, False)
    recalculated(w, run_id)
    assert amount(w, run_id, "Hal", "HOURLY_PAY") == D(20) * 8 * (wk - 1)
    # Another policy: only PUBLIC holidays count as paid holidays. This COMPANY holiday still closes the office
    # (day facts decide working, LH 05 §5.11, BUG-11 / IG-LH-20): an unpaid HOLIDAY day, never presumed present
    # and not paid by the hourly holiday rule (GV-09) -> 20.00 x 8 x (W - 1).
    activate_policy(w["admin"], "HOLIDAY_PAY_POLICY", {"non_salaried_holiday_pay": "STANDARD_HOURS",
                                                        "paid_holiday_types": ["PUBLIC"]})
    switch(w, key, True)
    recalculated(w, run_id)
    assert amount(w, run_id, "Hal", "HOURLY_PAY") == D(20) * 8 * (wk - 1), describe(w, run_id, "Hal")
    _, detail = lines_of(w, run_id, "Hal")
    day = {d["date"]: d for d in detail["ledger"]["days"]}[holiday_day.isoformat()]
    assert day["scheduled_working_day"] is False and day["holiday_pay_eligible"] is False, day
    assert {s["rule"] for s in day["slots"]} == {"4"} and {s["paid"] for s in day["slots"]} == {"0"}, day
    activate_policy(w["admin"], "HOLIDAY_PAY_POLICY", {"non_salaried_holiday_pay": "STANDARD_HOURS"})
    switch(w, key, False, "PAY_GROUP", w["groups"]["PG-B"])
    recalculated(w, run_id)
    assert amount(w, run_id, "Hal", "HOURLY_PAY") == D(20) * 8 * (wk - 1)    # PG-B override OFF wins
    # Salaried Ben (PG-A) is paid the holiday as part of salary either way (not a switch).
    ben_run = fresh_run(w, "PG-A")
    assert basic(w, ben_run, "Ben") == monthly_basic(w["period"])
    assert_bystander_unchanged(settings_world, (key, False))


# ---------------------------------------------------------------------------
# Payslip options (3.1 §5.15): the generated payslip follows the resolved values
# ---------------------------------------------------------------------------
def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find(value, key)
            if found is not None:
                return found
    return None


def _finalized(w, group_code, entity_code):
    from tests.payroll.test_run_finalize import ATTESTATIONS, approve, finalize, step_up, submit
    from tests.payroll.verification.conftest import second_admin

    if entity_code not in w.setdefault("attested", set()):
        attestation = ok(w["admin"].post(ATTESTATIONS, json={
            "legal_entity_id": w["entities"][entity_code], "effective_from": "2020-01-01",
            "reason": "Statutory filings handled outside this system"}), 201)
        ok(second_admin(w).post(f"{ATTESTATIONS}/{attestation['id']}/approve"))
        w["attested"].add(entity_code)
    run_id = fresh_run(w, group_code)
    submit(w["admin"], run_id)
    approve(w["approver"], run_id)
    step_up(w["admin"])
    finalize(w["admin"], run_id, key=f"fin-{run_id}")
    return run_id


def _payslip(w, run_id, person):
    listed = ok(w["admin"].get(f"{PAY}/payslips", params={"run_id": run_id}))
    items = listed["items"] if isinstance(listed, dict) else listed
    item = next(i for i in items if i.get("employee_id") == w["emp"][person]
                or (i.get("employee") or {}).get("id") == w["emp"][person])
    return ok(w["admin"].get(f"{PAY}/payslips/{item['id']}"))


def test_payslip_attendance_summary_and_ytd_options(settings_world):
    """Payslips are immutable once generated, so OFF and ON are compared across two pay groups finalized under
    different resolved values (tenant OFF, pay group PG-C overridden ON)."""
    w = settings_world["alpha"]
    summary, ytd = "payslip.show_attendance_summary", "payslip.show_ytd"
    for k in (summary, ytd):
        assert resolve(w["admin"], k, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is True
    switch(w, summary, False)
    switch(w, ytd, False)
    switch(w, summary, True, "PAY_GROUP", w["groups"]["PG-C"])
    off_run = _finalized(w, "PG-A", "VA1")
    on_run = _finalized(w, "PG-C", "VA2")
    ann, dee = _payslip(w, off_run, "Ann"), _payslip(w, on_run, "Dee")
    assert not _find(ann, "attendance"), ann                       # tenant OFF: no attendance summary
    assert _find(dee, "attendance"), dee                           # PG-C override ON: summary shown
    assert _find(ann, "show_ytd") is False and _find(dee, "show_ytd") is False
    assert_bystander_unchanged(settings_world, (summary, True), (ytd, True))


def test_payslip_publication_trigger_and_email_notification_options(settings_world):
    """Notification and document options (3.1 §5.15, 3.6): ``payslip.publish_trigger`` and
    ``payslip.email_notify`` are resolved for the run's entity and pay group. Tenant: publish only once the
    payment is submitted, no e-mail; pay group PG-C overrides both (publish on finalize, e-mail ON). Nothing is
    submitted for payment here, so PG-A's payslips must stay unpublished and un-notified."""
    from tests.payroll.verification.test_settings_inheritance import sql

    w = settings_world["alpha"]
    trigger, email = "payslip.publish_trigger", "payslip.email_notify"
    # (1) defaults: publish on finalize, e-mail ON.
    assert resolve(w["admin"], trigger, employee_id=w["emp"]["Ann"], on=w["end"])["value"] == "ON_FINALIZE"
    assert resolve(w["admin"], email, employee_id=w["emp"]["Ann"], on=w["end"])["value"] is True
    switch(w, trigger, "ON_PAYMENT_SUBMITTED")
    switch(w, email, False)
    switch(w, trigger, "ON_FINALIZE", "PAY_GROUP", w["groups"]["PG-C"])
    switch(w, email, True, "PAY_GROUP", w["groups"]["PG-C"])
    held_run = _finalized(w, "PG-A", "VA1")
    open_run = _finalized(w, "PG-C", "VA2")
    ann, dee = _payslip(w, held_run, "Ann"), _payslip(w, open_run, "Dee")
    # PG-A: generated but not published (no payment submitted), no notification on any channel.
    # (the document records the trigger that published it; an unpublished payslip has none yet)
    assert (ann["status"], ann["published_at"], ann["publish_trigger"]) == ("GENERATED", None, None), ann
    assert dee["publish_trigger"] == "ON_FINALIZE" and dee["published_at"], dee
    assert sql("SELECT count(*) FROM document_notifications WHERE document_id = :d", d=ann["id"])[0][0] == 0
    r = w["admin"].post(f"{PAY}/payslips/{ann['id']}/resend", headers=idem())
    assert r.status_code == 409 and r.json()["code"] == "NOT_PUBLISHED", r.text
    # PG-C: published at finalization, notified in the app and by e-mail (one row per channel).
    assert dee["status"] in ("PUBLISHED", "EMAIL_SENT", "EMAIL_FAILED", "VIEWED"), dee["status"]
    channels = sorted(c for (c,) in sql("SELECT channel FROM document_notifications WHERE document_id = :d",
                                        d=dee["id"]))
    assert channels == ["EMAIL", "IN_APP"], channels
    assert_bystander_unchanged(settings_world, (trigger, "ON_FINALIZE"), (email, True))


# ---------------------------------------------------------------------------
# Automatic arrears (3.1 §5.9, 3.3 §8.3): backdated salary change into a finalized period
# ---------------------------------------------------------------------------
ARREARS = f"{PAY}/arrears-proposals"


def revise(w, person, monthly):
    """A backdated compensation change from the period start (monthly CTC = ``monthly``), approved."""
    from tests.payroll.test_compensation import assign_and_approve

    assign_and_approve(w["admin"], w["approver"], w["emp"][person], w["structure_version"], str(monthly * 12),
                       effective_from=w["start"], currency="GBP")


def proposals_of(w, person):
    listed = ok(w["admin"].get(ARREARS, params={"employee_id": w["emp"][person], "limit": 200}))
    return listed["items"] if isinstance(listed, dict) else listed


def detect(w, person):
    return ok(w["admin"].post(f"{ARREARS}/detect", json={
        "employee_id": w["emp"][person], "trigger": "COMPENSATION", "from_date": w["start"].isoformat(),
        "reason": "Correction after payroll"}, headers=idem()))["items"]


def test_automatic_arrears_cycle(settings_world):
    from tests.helpers import create_employee
    from tests.payroll.test_compensation import login_as
    from tests.payroll.verification.conftest import periods_of

    w = settings_world["alpha"]
    # Ava approves the salary changes, so the arrears proposals they trigger are hers: another approver decides.
    checker = login_as(w["app"], w["admin"], create_employee(w["admin"], "Oli", "Approver"), ["payroll_approver"])
    key, mode = "payroll.arrears.auto_compute", "payroll.arrears.payment_mode"
    assert resolve(w["admin"], key, employee_id=w["emp"]["Dee"], on=w["end"])["value"] is True   # starter ON
    assert resolve(w["admin"], mode, employee_id=w["emp"]["Dee"], on=w["end"])["value"] == "NEXT_REGULAR_RUN"
    month = monthly_basic(w["period"])
    _finalized(w, "PG-C", "VA2")                                   # Dee paid month for the period
    # (4) +310.00 a month from the period start: the approval proposes the arrears by itself.
    revise(w, "Dee", month + D("310.00"))
    [proposal] = [p for p in proposals_of(w, "Dee") if p["status"] == "PENDING_APPROVAL"]
    ok(checker.post(f"{ARREARS}/{proposal['id']}/approve", json={}, headers=idem()))
    nxt = next(p for p in periods_of(w["admin"], w["groups"]["PG-C"]) if p["start"] > w["period"]["end"])
    run = create_run(w["admin"], w["groups"]["PG-C"], nxt["id"])
    precheck(w["admin"], run["id"])
    calculate(w["admin"], run["id"])
    run_id = run["id"]
    # (5)-(6) next period: the new monthly BASIC plus the period's arrears 310.00 (once).
    lines = lines_of(w, run_id, "Dee")[0]
    assert (lines.get("BASIC"), lines.get("ARREARS")) == (month + D("310.00"), D("310.00")), describe(w, run_id, "Dee")
    # (7)-(9) OFF: a new detection proposes nothing (ARREARS_AUTO_OFF: enter a wage input instead); the approved
    # arrears stay payable.
    switch(w, key, False)
    [item] = detect(w, "Dee")
    assert item["proposal_id"] is None and item["issues"] == ["ARREARS_AUTO_OFF"], item
    recalculated(w, run_id)
    assert amount(w, run_id, "Dee", "ARREARS") == D("310.00")
    # (10)-(11) ON with payment mode OFF_CYCLE: a further +100.00 is proposed but waits for a retro run.
    switch(w, key, True)
    switch(w, mode, "OFF_CYCLE")
    revise(w, "Dee", month + D("410.00"))
    [pending] = [p for p in proposals_of(w, "Dee") if p["status"] == "PENDING_APPROVAL"]
    ok(checker.post(f"{ARREARS}/{pending['id']}/approve", json={}, headers=idem()))
    recalculated(w, run_id)
    lines = lines_of(w, run_id, "Dee")[0]
    assert (lines.get("BASIC"), lines.get("ARREARS")) == (month + D("410.00"), D("310.00")), describe(w, run_id, "Dee")
    # Detecting again finds no new difference against the approved proposals (never paid twice).
    assert [i["status"] for i in detect(w, "Dee")] == ["NO_DIFFERENCE"]
    # (12)-(13) entity override OFF for VA2: Dee's next change proposes nothing; Ann (VA1) still gets one.
    switch(w, key, False, "LEGAL_ENTITY", w["entities"]["VA2"])
    before = len(proposals_of(w, "Dee"))
    revise(w, "Dee", month + D("460.00"))
    assert len(proposals_of(w, "Dee")) == before
    _finalized(w, "PG-A", "VA1")
    revise(w, "Ann", month + D("200.00"))
    assert [p["status"] for p in proposals_of(w, "Ann")] == ["PENDING_APPROVAL"]
    assert_bystander_unchanged(settings_world, (key, True))


# ---------------------------------------------------------------------------
# Payroll notifications (3.1 §5.17 notify.payroll.*, 3.8 §12, 3.7 §4 and §10; FOLLOW-UPS F-68)
# ---------------------------------------------------------------------------
# The five keys are TENANT-only (or fixed), so step (12) "override at another permitted level" is the product
# refusing every other level (LEVEL_NOT_ALLOWED / FIXED_SETTING) and step (13) checks the bystander tenant. A
# notification is counted in two independent places: the in-app ``notifications`` rows of each user and the
# e-mails the test e-mail adapter wrote for the tenant. Amounts are seeded with distinctive digits so that a
# leaked amount would be found in any message.
NOTIFY_REASON = "Verification: notification cycle"


def _sql(query, **params):
    from tests.payroll.verification.test_settings_inheritance import sql

    return sql(query, **params)


def user_of(client):
    """The user id of a signed-in client (the founder's client from signup carries none)."""
    if getattr(client, "user_id", None):
        return client.user_id
    client.user_id = client.me()["user"]["id"]
    return client.user_id


def email_of(user_id):
    return _sql("SELECT lower(email) FROM users WHERE id = :u", u=user_id)[0][0]


def notes_of(user_id, title_like="%"):
    """The user's in-app notifications whose title matches (oldest first)."""
    return [{"title": t, "message": m, "url": u} for t, m, u in _sql(
        "SELECT title, message, action_url FROM notifications WHERE user_id = :u AND title LIKE :t ORDER BY id",
        u=user_id, t=title_like)]


def tenant_notes(company_id, title_like):
    return _sql("SELECT count(*) FROM notifications WHERE company_id = :c AND title LIKE :t", c=company_id,
                t=title_like)[0][0]


def mails_of(company_id, template):
    """E-mails of one template written by the test e-mail adapter for the tenant: [{to, subject, text, html}]."""
    from tests.payroll.test_bank_accounts import mails

    return [{**m, "to": m["to"].lower()} for m in mails(company_id, template)]


def clear_mails(company_id, *templates):
    from tests.payroll.test_bank_accounts import outbox_files

    for template in templates:
        for path in outbox_files(company_id, template):
            path.unlink()


def refused_write(client, key, value, scope_type="TENANT", scope_id=None):
    """A settings PUT the product must refuse (400); returns the error code."""
    from tests.payroll.verification.conftest import VALUES

    change = {"key": key, "value": value, "scope_type": scope_type, "row_version": 0}
    if scope_id is not None:
        change["scope_id"] = scope_id
    r = client.put(VALUES, json={"changes": [change]})
    assert r.status_code == 400, f"{key}={value!r} at {scope_type}: {r.status_code} {r.text}"
    return r.json()["code"]


def run_daily_job(company_id, job_type, day):
    """Run one of the tenant's daily notification jobs for ``day`` through the jobs table. The scheduler queues it
    at 08:00 company time (``notifications.schedule_daily``); here it is queued due now under its own key, and the
    inline job mode runs it, then the events and e-mails it queues. Every notification job must succeed."""
    from app.database.db import SessionLocal, bind_tenant
    from app.payroll.services import jobs

    db = SessionLocal()
    try:
        bind_tenant(db, company_id)
        job_id = jobs.enqueue(db, company_id=company_id, type=job_type, payload={"run_date": day.isoformat()},
                              idempotency_key=f"verify:{day.isoformat()}:{idem()['Idempotency-Key'][-10:]}").id
        db.commit()
    finally:
        db.close()
    failed = _sql("SELECT type, status, error FROM jobs WHERE company_id = :c AND type LIKE 'payroll.%' AND "
                  "(type LIKE 'payroll.notify%' OR id = :j) AND status <> 'SUCCEEDED'", c=company_id, j=job_id)
    assert failed == [], failed
    return _sql("SELECT result FROM jobs WHERE id = :j", j=job_id)[0][0]


def no_notify_job_failed(*company_ids):
    failed = _sql("SELECT company_id, type, status, error FROM jobs WHERE company_id = ANY(:c) AND "
                  "type LIKE 'payroll.notify%' AND status <> 'SUCCEEDED'", c=list(company_ids))
    assert failed == [], failed


def test_approval_requested_notification_cycle(settings_world):
    """notify.payroll.approval_requested (channels IN_APP and EMAIL; an empty set is OFF). The subject is a bonus
    waiting for approval (a payroll input): the founder enters it and, as its maker, may not approve it, so the only
    eligible approver is the second administrator Sam (payroll_manager). Step (3) does not apply (no policy)."""
    w, beta = settings_world["alpha"], settings_world["beta"]
    key, template, waiting = "notify.payroll.approval_requested", "payroll.approval_requested", "%awaiting your approval"
    for world in (w, beta):
        clear_mails(world["cid"], template)
    sam = second_admin(w)
    sam_mail = email_of(sam.user_id)
    # (1) starter value: both channels.
    assert resolve(w["admin"], key, employee_id=w["emp"]["Ben"], on=w["end"])["value"] == ["IN_APP", "EMAIL"]
    # (4)-(6) ON (both): Sam is told in the app and by e-mail, once, with the request number and no amount.
    first = wage_input(w, "Ben", "BONUS", "4321.98", approve=False)
    assert first["status"] == "PENDING_APPROVAL", first
    rid = first["approval_request_id"]
    [note] = notes_of(sam.user_id, waiting)
    assert (note["title"], note["url"]) == ("Payroll input awaiting your approval", f"/payroll/approvals?id={rid}")
    assert f"#{rid}" in note["message"] and "4321" not in note["message"] and "4,321" not in note["message"]
    [mail] = mails_of(w["cid"], template)
    assert (mail["to"], mail["subject"]) == (sam_mail, "Payroll input awaiting your approval"), mail
    assert f"#{rid}" in mail["text"] and not any(s in mail["text"] + mail["html"] for s in ("4321", "4,321"))
    # Nobody else may decide it, so nobody else hears of it (the founder is the maker; Ava and Max lack the right).
    # (The world's setup sent Ava compensation approvals; only messages about this request count.)
    for user_id in (w["approver"].user_id, w["manager"].user_id, user_of(w["admin"])):
        assert [n for n in notes_of(user_id, waiting) if n["url"] == f"/payroll/approvals?id={rid}"] == []
    approve_request(w, rid, deciders=("second_admin",))
    # (7)-(9) OFF (no channel): the next input waits for Sam as before, but nobody is told on any channel.
    put_setting(w["admin"], key, [], reason=NOTIFY_REASON)
    assert resolve(w["admin"], key, employee_id=w["emp"]["Cal"], on=w["end"])["value"] == []
    second = wage_input(w, "Cal", "BONUS", "1357.24", approve=False)
    assert second["status"] == "PENDING_APPROVAL", second
    assert len(notes_of(sam.user_id, waiting)) == 1 and len(mails_of(w["cid"], template)) == 1
    approve_request(w, second["approval_request_id"], deciders=("second_admin",))   # the approval itself works
    # (10)-(11) ON with another value: e-mail only.
    put_setting(w["admin"], key, ["EMAIL"], reason=NOTIFY_REASON)
    third = wage_input(w, "Ann", "BONUS", "2468.13", approve=False)
    assert len(notes_of(sam.user_id, waiting)) == 1
    mails = mails_of(w["cid"], template)
    assert len(mails) == 2 and mails[-1]["to"] == sam_mail and f"#{third['approval_request_id']}" in mails[-1]["text"]
    assert "2468" not in mails[-1]["text"] + mails[-1]["html"]
    # (12) TENANT is the only level of this key.
    for scope_type, scope_id in (("LEGAL_ENTITY", w["entities"]["VA1"]), ("PAY_GROUP", w["groups"]["PG-A"])):
        assert refused_write(w["admin"], key, ["IN_APP"], scope_type, scope_id) == "LEVEL_NOT_ALLOWED"
    # (13) Verity Beta: its value never moved and its users heard nothing of Alpha's requests.
    assert tenant_notes(beta["cid"], "Payroll input awaiting%") == 0 and mails_of(beta["cid"], template) == []
    no_notify_job_failed(w["cid"], beta["cid"])
    assert_bystander_unchanged(settings_world, (key, ["IN_APP", "EMAIL"]))


def test_input_cutoff_reminder_cycle(settings_world):
    """notify.payroll.input_cutoff_reminder_days (0-31 days before the input cutoff; no OFF value exists in the
    registry, so steps (7)-(9) use 0 = "on the cutoff day" and show that no reminder comes earlier). The three pay
    groups share one monthly calendar, so each future period of PG-A has twins in PG-B and PG-C: one daily job
    reminds about all three. Recipients by hand: the founder holds payroll:inputs:edit company-wide (staff
    message, link to inputs); Max manages every placed employee (manager message, link to the approvals inbox);
    Ava (payroll_approver) has no input right and manages nobody. Step (3) does not apply (no policy)."""
    from datetime import date

    from tests.payroll.verification.conftest import periods_of

    w, beta = settings_world["alpha"], settings_world["beta"]
    key, job, template = ("notify.payroll.input_cutoff_reminder_days", "payroll.input_cutoff_reminders",
                          "payroll.input_cutoff_reminder")
    for world in (w, beta):
        clear_mails(world["cid"], template)

    def cutoffs(world, code):
        later = [p for p in periods_of(world["admin"], world["groups"][code]) if p["start"] > world["end"].isoformat()]
        return [date.fromisoformat(p["input_cutoff"]) for p in later[:3]]

    cut = cutoffs(w, "PG-A")
    assert cutoffs(w, "PG-B") == cut and cutoffs(w, "PG-C") == cut
    founder, boss, ava = user_of(w["admin"]), w["manager"].user_id, w["approver"].user_id
    reminders = lambda uid: notes_of(uid, "Payroll inputs close on%")  # noqa: E731
    # (1) default 2 days.
    assert resolve(w["admin"], key, pay_group_id=w["groups"]["PG-A"], on=w["end"])["value"] == 2
    # (5) three days before the first cutoff: nothing yet.
    run_daily_job(w["cid"], job, cut[0] - timedelta(days=3))
    assert reminders(founder) == [] and reminders(boss) == []
    # (6) two days before: one reminder per pay group to the founder and to Max, and an e-mail each.
    run_daily_job(w["cid"], job, cut[0] - timedelta(days=2))
    title = f"Payroll inputs close on {cut[0]:%d %b %Y}"
    staff, team = reminders(founder), reminders(boss)
    assert [n["title"] for n in staff] == [title] * 3 and [n["title"] for n in team] == [title] * 3
    assert {n["url"] for n in staff} == {"/payroll/inputs"} and {n["url"] for n in team} == {"/payroll/approvals"}
    assert all(n["message"].startswith("Inputs for pay group") for n in staff)
    assert all(n["message"].startswith("Payroll inputs for your team") for n in team)
    assert reminders(ava) == []
    mails = mails_of(w["cid"], template)
    assert sorted(m["to"] for m in mails) == sorted([email_of(founder)] * 3 + [email_of(boss)] * 3)
    assert {m["subject"] for m in mails} == {title}
    # The window's next day: each period is reminded once (no duplicate).
    run_daily_job(w["cid"], job, cut[0] - timedelta(days=1))
    assert len(reminders(founder)) == 3 and len(reminders(boss)) == 3 and len(mails_of(w["cid"], template)) == 6
    # (7)-(9) 0 days: nothing two days before the second cutoff, the reminder comes on the cutoff day.
    put_setting(w["admin"], key, 0, reason=NOTIFY_REASON)
    run_daily_job(w["cid"], job, cut[1] - timedelta(days=2))
    assert len(reminders(founder)) == 3 and len(reminders(boss)) == 3
    run_daily_job(w["cid"], job, cut[1])
    second = f"Payroll inputs close on {cut[1]:%d %b %Y}"
    assert [n["title"] for n in reminders(founder)[3:]] == [second] * 3
    assert [n["title"] for n in reminders(boss)[3:]] == [second] * 3
    # (10)-(11) 5 days: not six days before the third cutoff, five days before it.
    put_setting(w["admin"], key, 5, reason=NOTIFY_REASON)
    run_daily_job(w["cid"], job, cut[2] - timedelta(days=6))
    assert len(reminders(founder)) == 6
    run_daily_job(w["cid"], job, cut[2] - timedelta(days=5))
    third = f"Payroll inputs close on {cut[2]:%d %b %Y}"
    assert [n["title"] for n in reminders(founder)[6:]] == [third] * 3
    assert [n["title"] for n in reminders(boss)[6:]] == [third] * 3 and len(mails_of(w["cid"], template)) == 18
    # (12) TENANT is the only level of this key.
    assert refused_write(w["admin"], key, 7, "PAY_GROUP", w["groups"]["PG-A"]) == "LEVEL_NOT_ALLOWED"
    # (13) Verity Beta keeps 2 days: Alpha's jobs told it nothing; its own job reminds its own people only.
    assert tenant_notes(beta["cid"], "Payroll inputs close on%") == 0 and mails_of(beta["cid"], template) == []
    beta_cut = cutoffs(beta, "PG-Z")[0]
    run_daily_job(beta["cid"], job, beta_cut - timedelta(days=5))
    assert tenant_notes(beta["cid"], "Payroll inputs close on%") == 0
    run_daily_job(beta["cid"], job, beta_cut - timedelta(days=2))
    assert [n["title"] for n in reminders(user_of(beta["admin"]))] == [f"Payroll inputs close on {beta_cut:%d %b %Y}"]
    assert [n["title"] for n in reminders(beta["manager"].user_id)] == [
        f"Payroll inputs close on {beta_cut:%d %b %Y}"]
    assert len(reminders(founder)) == 9                                   # Alpha unchanged by Beta's job
    assert_bystander_unchanged(settings_world, (key, 2))


def test_exceptions_digest_cycle(settings_world):
    """notify.payroll.exceptions_digest (DAILY / OFF). Seed: Ann's only bank account is unverified, so the draft
    PG-A run has open findings (UNVERIFIED_BANK, UNVERIFIED_ACCOUNT_ON_HOLD, ...). The digest must count exactly what
    the exceptions API lists as OPEN, by severity, and name the run, never an amount or an exception text.
    Recipients by hand: holders of payroll:run:create covering the run = the founder only (Ava approves, Max
    manages). The enum has two values, so step (10) is DAILY again on a new date; step (3) does not apply."""
    from tests.payroll.test_payment_obligations import add_bank_account

    w, beta = settings_world["alpha"], settings_world["beta"]
    key, job, template = "notify.payroll.exceptions_digest", "payroll.exceptions_digest", "payroll.exceptions_digest"
    for world in (w, beta):
        clear_mails(world["cid"], template)
    founder = user_of(w["admin"])
    digests = lambda uid: notes_of(uid, "Payroll exceptions:%")  # noqa: E731
    # (1) default DAILY.
    assert resolve(w["admin"], key, pay_group_id=w["groups"]["PG-A"], on=w["end"])["value"] == "DAILY"
    # (4) seed open exceptions.
    add_bank_account(w["cid"], w["emp"]["Ann"], "50000012345678", verified=False, holder="Ann Verify", currency="GBP")
    run_id = fresh_run(w, "PG-A")
    found = open_findings(w["admin"], run_id)
    assert found and [f for f in found if f["code"] == "UNVERIFIED_BANK"], found
    total, blocking = len(found), sum(1 for f in found if f["severity"] == "BLOCKING")
    number = run_detail(w["admin"], run_id)["number"]
    today = today_of(w["admin"])
    days = [today + timedelta(days=n) for n in (1, 2, 3)]
    # (5)-(6) DAILY: one digest for the founder, in the app and by e-mail.
    run_daily_job(w["cid"], job, days[0])
    [note] = digests(founder)
    assert note["title"] == f"Payroll exceptions: {total} open", (note, found)
    assert note["message"].startswith(f"{total} open exception{'s' if total != 1 else ''} ({blocking} blocking) in "
                                      "1 payroll run."), note
    assert number in note["message"] and note["url"] == f"/payroll/runs/{run_id}"
    month = monthly_basic(w["period"])
    for leaked in [f["message"] for f in found] + [str(month), f"{month:,}", "50000012345678", "5678"]:
        assert leaked not in note["message"], leaked
    [mail] = mails_of(w["cid"], template)
    assert (mail["to"], mail["subject"]) == (email_of(founder), note["title"]) and number in mail["text"]
    assert str(month) not in mail["text"] and "5678" not in mail["text"]
    assert digests(w["approver"].user_id) == [] and digests(w["manager"].user_id) == []
    # The same date again: no second digest.
    run_daily_job(w["cid"], job, days[0])
    assert len(digests(founder)) == 1 and len(mails_of(w["cid"], template)) == 1
    # (7)-(9) OFF: the next day sends nothing although the exceptions are still open.
    put_setting(w["admin"], key, "OFF", reason=NOTIFY_REASON)
    run_daily_job(w["cid"], job, days[1])
    assert len(digests(founder)) == 1 and len(mails_of(w["cid"], template)) == 1
    # (10)-(11) DAILY again: the day after, a new digest with the counts of that day.
    put_setting(w["admin"], key, "DAILY", reason=NOTIFY_REASON)
    run_daily_job(w["cid"], job, days[2])
    assert [n["title"] for n in digests(founder)] == [note["title"]] * 2 and len(mails_of(w["cid"], template)) == 2
    # (12) TENANT is the only level of this key.
    assert refused_write(w["admin"], key, "OFF", "PAY_GROUP", w["groups"]["PG-A"]) == "LEVEL_NOT_ALLOWED"
    # (13) Verity Beta: Alpha's digests never reached it; its own digest counts only its own run.
    assert tenant_notes(beta["cid"], "Payroll exceptions:%") == 0 and mails_of(beta["cid"], template) == []
    beta_found = open_findings(beta["admin"], beta["run"])
    run_daily_job(beta["cid"], job, days[0])
    beta_digest = digests(user_of(beta["admin"]))
    if beta_found:
        assert [n["title"] for n in beta_digest] == [f"Payroll exceptions: {len(beta_found)} open"]
        assert run_detail(beta["admin"], beta["run"])["number"] in beta_digest[0]["message"]
    else:
        assert beta_digest == []
    assert len(digests(founder)) == 2
    assert_bystander_unchanged(settings_world, (key, "DAILY"))


def test_payment_status_to_employee_cycle(app):
    """notify.payroll.payment_status_to_employee (bool). Payments go through the sandbox provider; the provider's
    settlement and failure reports are applied with the evidence hook (``apply_evidence``), as a webhook would. The
    bool has no second policy, so step (10) is ON again with new payments; step (3) does not apply. Account
    numbers and amounts are seeded so that a leak would show: 1,234.56 to Emma's account ending 4567."""
    from tests.payroll.test_bank_accounts import MASK
    from tests.payroll.test_compensation import login_as
    from tests.payroll.test_payment_events import evidence, sandbox_world
    from tests.payroll.test_payment_obligations import NUMBERS, instructions_of, payments_world

    key, template, alert = "notify.payroll.payment_status_to_employee", "payroll.payment_status", \
        "payroll.bank_change_alert"
    world = payments_world(app, "Status Pay")                              # Emma, Ivan, Zara, Nell
    bystander = payments_world(app, "Status Bystander", employees=("Emma",))
    admin, cid = world["admin"], world["cid"]
    for c in (cid, bystander["cid"]):
        clear_mails(c, template, alert)
    users = {n: login_as(app, admin, e, ["employee"]).user_id for n, e in world["emp"].items()}
    address = {n: e["email"].lower() for n, e in world["emp"].items()}
    credited = lambda name: notes_of(users[name], "Salary credited")  # noqa: E731
    failed = lambda name: notes_of(users[name], "Payment failed%")  # noqa: E731
    # (1) default ON.
    assert resolve(admin, key, employee_id=world["emp"]["Emma"]["id"])["value"] is True
    # (4) a finalized run paid through the sandbox: four submitted payments.
    run_id, _ = sandbox_world(world, {"Emma": "1234.56", "Ivan": "2345.67", "Zara": "3456.78", "Nell": "4567.89"})
    rows = instructions_of(run_id)
    ids = {n: rows[world["emp"][n]["id"]]["id"] for n in world["emp"]}
    # (5)-(6) Emma's payment settles: "Salary credited" once, in the app and by e-mail, masked, without the amount.
    assert evidence(cid, ids["Emma"], "SETTLED", source="WEBHOOK", event_id="ps-emma-1", sandbox=True)[0] == "APPLIED"
    [note] = credited("Emma")
    assert f"{MASK}{NUMBERS['Emma'][-4:]}" in note["message"] and note["url"] == "/me/payments"
    for leaked in (NUMBERS["Emma"], "1234", "1,234"):
        assert leaked not in note["message"], leaked
    [mail] = mails_of(cid, template)
    assert (mail["to"], mail["subject"]) == (address["Emma"], "Salary credited")
    assert f"{MASK}{NUMBERS['Emma'][-4:]}" in mail["text"]
    assert not any(s in mail["text"] + mail["html"] for s in (NUMBERS["Emma"], "1234", "1,234"))
    # The provider reports the settlement again: no second message.
    assert evidence(cid, ids["Emma"], "SETTLED", source="WEBHOOK", event_id="ps-emma-1", sandbox=True)[0] == "DUPLICATE"
    evidence(cid, ids["Emma"], "SETTLED", source="WEBHOOK", event_id="ps-emma-2", sandbox=True)
    assert len(credited("Emma")) == 1 and len(mails_of(cid, template)) == 1
    # (7)-(9) OFF: Ivan's settlement and Zara's failure tell nobody; Zara's account still becomes FAILED.
    put_setting(admin, key, False, reason=NOTIFY_REASON)
    assert evidence(cid, ids["Ivan"], "SETTLED", source="WEBHOOK", event_id="ps-ivan-1", sandbox=True)[0] == "APPLIED"
    assert evidence(cid, ids["Zara"], "FAILED", source="WEBHOOK", event_id="ps-zara-1", sandbox=True,
                    reason_code="AC04")[0] == "APPLIED"
    assert credited("Ivan") == [] and failed("Zara") == [] and mails_of(cid, template) == [mail]
    assert [m for m in mails_of(cid, alert) if m["to"] == address["Zara"]] == []
    assert _sql("SELECT verification_status FROM bank_accounts WHERE employee_id = :e",
                e=world["emp"]["Zara"]["id"])[0][0] == "FAILED"
    # (10)-(11) ON again: Nell's failed payment asks her (masked) to update her bank details.
    put_setting(admin, key, True, reason=NOTIFY_REASON)
    assert evidence(cid, ids["Nell"], "FAILED", source="WEBHOOK", event_id="ps-nell-1", sandbox=True,
                    reason_code="AC04")[0] == "APPLIED"
    [nell] = failed("Nell")
    assert nell["title"] == "Payment failed — please update your bank details", nell
    assert f"{MASK}{NUMBERS['Nell'][-4:]}" in nell["message"] and NUMBERS["Nell"] not in nell["message"]
    assert "4567.89" not in nell["message"] and "4,567" not in nell["message"]
    nell_mails = [m for m in mails_of(cid, alert) if m["to"] == address["Nell"]]
    assert len(nell_mails) == 1 and nell_mails[0]["subject"].startswith("Payment failed"), nell_mails
    assert NUMBERS["Nell"] not in nell_mails[0]["text"]
    # (12) TENANT is the only level of this key.
    assert refused_write(admin, key, False, "LEGAL_ENTITY", world["le"]) == "LEVEL_NOT_ALLOWED"
    # (13) the bystander tenant: still ON, nothing of Alpha's payments reached it, its own settlement tells its
    # own employee only.
    assert resolve(bystander["admin"], key, employee_id=bystander["emp"]["Emma"]["id"])["value"] is True
    assert tenant_notes(bystander["cid"], "Salary credited") == 0 and mails_of(bystander["cid"], template) == []
    other = login_as(app, bystander["admin"], bystander["emp"]["Emma"], ["employee"]).user_id
    other_run, _ = sandbox_world(bystander, {"Emma": "100.00"})
    other_id = instructions_of(other_run)[bystander["emp"]["Emma"]["id"]]["id"]
    evidence(bystander["cid"], other_id, "SETTLED", source="WEBHOOK", event_id="ps-by-1", sandbox=True)
    assert len(notes_of(other, "Salary credited")) == 1 and len(credited("Emma")) == 1
    assert [m["to"] for m in mails_of(bystander["cid"], template)] == [bystander["emp"]["Emma"]["email"].lower()]
    no_notify_job_failed(cid, bystander["cid"])


def test_bank_change_alert_is_locked_on_cycle(bank_world):
    """notify.payroll.bank_change_alert is locked ON: the registry fixes it to True and allows no level, so steps
    (2), (7), (10) and (12) are refused writes (FIXED_SETTING) and step (3) does not apply. Step (8) goes further
    than the API allows: a FALSE row is written into setting_values directly, and the alert still goes to the old
    and new contact details (the service reads the key fail-safe)."""
    from tests.payroll.test_bank_accounts import IN_NUMBER, MASK, active_account, clear_outbox, in_account, mails
    from tests.payroll.test_bank_accounts import step_up, submit

    w = bank_world
    cid, emma, founder, beta = w["cid"], w["u"]["emma"], w["u"]["founder"], w["u"]["beta"]
    key = "notify.payroll.bank_change_alert"
    # (1) effective value: ON.
    assert resolve(founder, key)["value"] is True
    # (2) and (7): neither ON nor OFF can be stored at TENANT; (12): nor at an entity.
    for value in (True, False):
        assert refused_write(founder, key, value) == "FIXED_SETTING"
    assert refused_write(founder, key, False, "LEGAL_ENTITY", w["le01"]["id"]) == "FIXED_SETTING"
    # (4)-(6) Emma changes her salary account: the request and the change are announced, masked, in the app and
    # by e-mail.
    clear_outbox(cid)
    clear_outbox(beta.company_id)
    active_account(w, emma, in_account())
    subjects = sorted(m["subject"] for m in mails(cid))
    assert subjects == ["Bank account change requested", "Your salary bank account was changed"], subjects
    assert all(f"{MASK}6789" in m["text"] and IN_NUMBER not in m["text"] for m in mails(cid))
    titles = [n["title"] for n in notes_of(emma.user_id, "%ank account%")]
    assert "Bank account change requested" in titles and "Your salary bank account was changed" in titles
    # (8)-(9) a FALSE value forced past the API never silences the alert.
    _sql("INSERT INTO setting_values (company_id, key, scope_type, scope_id, value, locked, effective_from, status, "
         "row_version, source, created_at, updated_at) VALUES (:c, :k, 'TENANT', NULL, 'false'::jsonb, false, "
         "DATE '2020-01-01', 'ACTIVE', 1, 'ADMIN', now(), now())", c=cid, k=key)
    clear_outbox(cid)
    step_up(emma)
    submit(emma, in_account(number="50100123456784"))
    alerts = mails(cid)
    assert alerts and all(m["subject"] == "Bank account change requested" for m in alerts), alerts
    assert all(f"{MASK}6784" in m["text"] and "50100123456784" not in m["text"] for m in alerts)
    # (10)-(11) switching it "back on" is refused as well: there is nothing to switch.
    assert refused_write(founder, key, True) == "FIXED_SETTING"
    # (13) Beta: ON, and none of Acme's alerts reached it.
    assert resolve(beta, key)["value"] is True and mails(beta.company_id) == []
    assert tenant_notes(beta.company_id, "%ank account%") == 0


# ---------------------------------------------------------------------------
# Features that cannot be verified through a run here (precise reasons; the list itself is asserted)
# ---------------------------------------------------------------------------
BLOCKED_FEATURES = {
    "payroll.advances.enabled (recovery in a run)": "An advance application needs the monthly net pay of a finalized "
    "regular run whose period has already ended (snapshot.pay_figures filters period_end <= today) and refuses with "
    "ADVANCE_NET_UNKNOWN otherwise; the verification tenants are provisioned inside the current period, so no "
    "advance can be approved on the run date. The switch gate itself is verified "
    "(test_salary_advance_switch_gates_applications).",
    "employee benefits (Goal 2 §6)": "Payroll v2 has no benefit setting and no benefit policy type: the design "
    "(3.1 §4 switch catalogue, §3.2 policy types) and the registry define none, so there is nothing to switch ON or "
    "OFF (decision I-21). A benefit is a salary component of kind BENEFIT_IN_KIND or an EMPLOYER_CONTRIBUTION of "
    "category BENEFIT; test_incentives_and_benefits.py proves they stay out of cash gross and net and appear on the "
    "payslip and in the CTC breakdown.",
}


def test_blocked_features_are_really_not_consumed_by_any_service():
    """Keeps BLOCKED_FEATURES honest: if a service starts reading one of these keys, move it into a cycle test."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "app"
    sources = {p: p.read_text(encoding="utf-8") for p in root.rglob("*.py") if p.name != "settings_registry.py"}
    for key in (k for k in BLOCKED_FEATURES if " " not in k):
        readers = [str(p) for p, text in sources.items() if f'"{key}"' in text or f"'{key}'" in text]
        readers = [p for p in readers if not p.endswith("policy_types.py")]
        assert readers == [], f"{key} is read by {readers}: verify it with a real ON/OFF cycle"
