"""LH-owned halves of the leave / holiday x shift / roster edge cases — pure parts (no database).

* AS-INT-07 — ``WorkWeekResolver.matches`` matches ``shift_ids`` on the criterion shift of the date
  (``shift_resolution.criterion_shifts_many``), loaded once per primed day-facts batch.
* AS-INT-08 / X-INT-56 / X-INT-58 — ``calendar_version`` gains the JC-AS-30 keys ``roster``, ``shift_versions``,
  ``shift_rules`` per date, omitted when empty (zero diff); the recount note per key; the roster-publish preview's
  hypothetical day facts and their charge.

DB halves: tests/leave/test_lh_shift_integration.py.
"""

import datetime as dt
import os
from decimal import Decimal
from types import SimpleNamespace

for _key, _value in {"DATABASE_URL": "postgresql://x:y@localhost:1/z", "SYSTEM_DATABASE_URL":
                     "postgresql://x:y@localhost:1/z", "SECRET_KEY": "pure-test-secret-key-long-enough-for-hs256",
                     "ENVIRONMENT": "test"}.items():
    os.environ.setdefault(_key, _value)

from app.payroll.services import time_inputs as ti  # noqa: E402 - after the settings above
from app.services import (  # noqa: E402
    holiday_assignments, leave_charging, leave_jobs, leave_recalc, shift_resolution,
)

D = dt.date
SHIFT_A, SHIFT_C = 201, 203
REPUBLIC_DAY = D(2027, 1, 26)


def placement(employee_id=7, shift_id=SHIFT_A, start=D(2026, 1, 1), end=None):
    return SimpleNamespace(employee_id=employee_id, shift_id=shift_id, effective_from=start, effective_to=end)


# ------------------------------------------------------------------------------------------------ AS-INT-07
def test_as_int_07_shift_ids_matches_the_criterion_shift_not_the_placement_shift():
    """Ravi's placement shift is A; on Republic Day his HOLIDAY_RULE OFF entry stands for the night shift C it
    replaced (criterion shift C), so the night calendar's ``shift_ids [C]`` still matches and ``[A]`` does not. A MANUAL
    OFF date (criterion None) matches nothing; a date outside the placement keeps the placement's shift."""
    resolver = holiday_assignments.WorkWeekResolver(None, 1)
    resolver._criterion_shifts.update({(7, REPUBLIC_DAY): SHIFT_C, (7, REPUBLIC_DAY + dt.timedelta(days=1)): None})
    ravi = placement()
    assert resolver.matches({"shift_ids": [SHIFT_C]}, ravi, REPUBLIC_DAY)
    assert not resolver.matches({"shift_ids": [SHIFT_A]}, ravi, REPUBLIC_DAY)
    assert not resolver.matches({"shift_ids": [SHIFT_A, SHIFT_C]}, ravi, REPUBLIC_DAY + dt.timedelta(days=1))
    left = placement(end=D(2026, 12, 31))  # the nearest placement of a date after it ends (F-04)
    assert resolver.matches({"shift_ids": [SHIFT_A]}, left, REPUBLIC_DAY)


def test_as_int_07_a_primed_batch_loads_the_criterion_once(monkeypatch):
    calls = []

    def fake(db, company_id, ids, start, end):
        calls.append((list(ids), start, end))
        days = [start + dt.timedelta(days=n) for n in range((end - start).days + 1)]
        return {(e, d): SHIFT_C for e in ids for d in days}

    monkeypatch.setattr(shift_resolution, "criterion_shifts_many", fake)
    batch = holiday_assignments.WorkWeekResolver(None, 1)
    batch.prime_shift_criterion([7, 8], D(2027, 1, 1), D(2027, 1, 31))
    assert batch.criterion_shift(7, REPUBLIC_DAY) == SHIFT_C
    assert batch.criterion_shift(8, D(2027, 1, 31)) == SHIFT_C
    assert calls == [([7, 8], D(2027, 1, 1), D(2027, 1, 31))]
    alone = holiday_assignments.WorkWeekResolver(None, 1)  # unprimed: one employee, a 31-day chunk
    alone.criterion_shift(9, REPUBLIC_DAY)
    alone.criterion_shift(9, REPUBLIC_DAY + dt.timedelta(days=30))
    assert calls[1:] == [([9], REPUBLIC_DAY, REPUBLIC_DAY + dt.timedelta(days=30))]


# ------------------------------------------------------------------------------- AS-INT-08 / X-INT-56 / X-INT-58
def test_calendar_version_without_roster_or_rules_is_todays_hash(monkeypatch):
    """JC-AS-30: empty keys are omitted, so a tenant without rosters or rules keeps today's hash; the keys are
    order-independent (same inputs, same hash) and any entry / version / rule change gives a new hash."""
    parts = {"years": [[5, 2026, 2]], "patterns": [[3, 1]], "assignments": [["link", 9, 1]]}
    monkeypatch.setattr(ti, "_calendar_parts", lambda *a, **k: dict(parts))
    args = (None, 1, [5], [(3, 1)], [22], D(2026, 11, 1), D(2026, 11, 30))
    today = ti.content_hash({"years": [[5, 2026, 2]], "patterns": [[3, 1]], "assignments": [["link", 9, 1]]})
    assert ti._calendar_version(*args) == today
    assert ti._calendar_version(*args, roster=(), shift_versions=(), shift_rules=()) == today
    one = ti._calendar_version(*args, roster=[(11, 1), (12, 1)], shift_versions=[40], shift_rules=[(6, 2)])
    assert one == ti._calendar_version(*args, roster=[(12, 1), (11, 1)], shift_versions=[40, 40],
                                       shift_rules=[(6, 2)])
    assert len({today, one, ti._calendar_version(*args, roster=[(11, 2), (12, 1)], shift_versions=[40],
                                                 shift_rules=[(6, 2)]),
                ti._calendar_version(*args, roster=[(11, 1), (12, 1)], shift_versions=[41], shift_rules=[(6, 2)]),
                ti._calendar_version(*args, roster=[(11, 1), (12, 1)], shift_versions=[40], shift_rules=[(6, 3)])}) == 5
    assert ti._shift_parts() == {} and ti._shift_parts(roster=[(12, 1), (11, 3)]) == {"roster": [[11, 3], [12, 1]]}


def _batch(shifts, roster_marks=(), rule_versions=None):
    batch = object.__new__(ti._Batch)
    parts = {"years": [], "patterns": [[3, 1]], "assignments": []}
    base = ti.content_hash(parts)
    batch.shifts, batch.roster_marks, batch.rule_versions = shifts, dict(roster_marks), rule_versions or {}
    batch._parts = {base: parts}
    return batch, base


def test_day_version_adds_the_roster_entry_and_the_deciding_version_or_rule_per_date():
    """Per date, so the hash of a date never depends on the range asked: a rostered OFF (roster key only), a
    rostered WORK (entry + version), a rule-resolved shift (version + rule row_version); a placement shift adds
    nothing (zero diff). A republished entry (version + 1) or a re-saved rule gives a new hash."""
    day, other = D(2026, 11, 20), D(2026, 11, 21)
    placement_shift = SimpleNamespace(source="PLACEMENT", shift_version_id=40, rule_id=None)
    batch, base = _batch({(1, day): placement_shift, (2, day): None,
                          (3, day): SimpleNamespace(source="ROSTER", shift_version_id=41, rule_id=None),
                          (4, day): SimpleNamespace(source="RULE", shift_version_id=42, rule_id=6)},
                         roster_marks={((2, day)): ((70, 1),), ((3, day)): ((71, 2),)}, rule_versions={6: 3})
    assert batch.day_version(base, 1, day) == base and batch.day_version(base, 1, other) == base
    assert batch.day_version("", 3, day) == ""  # light day facts carry no version
    versions = {e: batch.day_version(base, e, day) for e in (2, 3, 4)}
    assert len(set(versions.values()) | {base}) == 4
    assert versions[3] == ti.content_hash({**batch._parts[base], "roster": [[71, 2]], "shift_versions": [[41]]})
    assert versions[4] == ti.content_hash({**batch._parts[base], "shift_versions": [[42]], "shift_rules": [[6, 3]]})
    republished, _ = _batch(batch.shifts, roster_marks={((3, day)): ((71, 3),)})
    republished._parts = batch._parts
    assert republished.day_version(base, 3, day) != versions[3]
    resaved, _ = _batch(batch.shifts, rule_versions={6: 4})
    resaved._parts = batch._parts
    assert resaved.day_version(base, 4, day) != versions[4]


def test_recount_notes_follow_the_change_that_triggered_them():
    assert leave_jobs._note("recalc:roster:41") == "Roster changed"
    assert leave_jobs._note("recalc:shift:shift_version:9:2") == "Shift times changed"
    assert leave_jobs._note("recalc:calendar:3:2:7") is None  # the default "Holiday calendar changed"


def _facts(day, *, off=False, holiday=None):
    portion = "FULL" if off else None
    return ti.DayFacts(day, True, None, 22, 3, off, holiday, weekly_off_portion=portion, rest_day=off,
                       working_portion=ti.working_portion(True, portion, holiday), scheduled_minutes=0 if off else 480)


def test_as_int_08_preview_day_facts_charge_old_and_new_units_without_the_database():
    """Sunita's Thu-Sat leave under WORKING_DAYS: Fri and Sat rostered OFF -> 1.00; Fri as WORK -> 2.00 (GV-AS-INT-04).
    A WORK override on a full holiday keeps it non-working; an OFF override releases a working day; a half-day
    request on the overridden date charges its half."""
    thu, fri, sat = D(2026, 11, 19), D(2026, 11, 20), D(2026, 11, 21)
    facts = [_facts(thu), _facts(fri, off=True), _facts(sat, off=True)]
    old, _ = leave_charging.charge(None, 1, 1, None, thu, sat, facts_list=facts)
    work = [leave_recalc._overridden(f, "WORK") if f.date == fri else f for f in facts]
    new, days = leave_charging.charge(None, 1, 1, None, thu, sat, facts_list=work)
    assert (old, new) == (Decimal(1), Decimal(2))
    assert [(d.date, d.charged, d.charge_reason) for d in days] == [
        (thu, 1, "WORKING"), (fri, 1, "WORKING"), (sat, 0, None)]
    assert (work[1].weekly_off, work[1].working_portion, work[1].work_source) == (False, "FULL", "ROSTER")
    closure = ti.HolidayFact(1, "Plant closure", "COMPANY", "h")
    on_holiday = leave_recalc._overridden(_facts(fri, holiday=closure), "WORK")
    assert (on_holiday.weekly_off, on_holiday.working_portion) == (False, "NONE")
    released = leave_recalc._overridden(_facts(thu), "OFF")
    assert (released.weekly_off, released.working_portion, released.scheduled_minutes) == (True, "NONE", 0)
    half, _ = leave_charging.charge(None, 1, 1, None, fri, fri, "FULL", "FIRST_HALF", facts_list=[work[1]])
    assert half == Decimal("0.5")
