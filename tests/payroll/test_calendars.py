"""Pay calendars and periods (3.5 §2, 3.9 §4 Calendars, §6 period generation job).

EC-CAL-01, EC-CAL-03, EC-CAL-06, EC-CAL-07, EC-CAL-08, EC-CAL-11, EC-CAL-12.
"""

from datetime import date, timedelta

import pytest

from tests.helpers import create_employee, signup_company
from tests.payroll.test_components import ok
from tests.payroll.test_runs_lifecycle import PAY, calendar_for, new_run, periods_of
from tests.payroll.test_wage_inputs import audit_actions, expect_error, pay_group, sql, tenant, today_of

EXCLUSION = "23P01"


@pytest.fixture
def acme(app):
    client = signup_company(app, "Acme Corp")
    cid = client.company_id
    entity = sql("SELECT id, country_code FROM legal_entities WHERE company_id = :c AND code = 'LE01'", c=cid)[0]
    client.entity, client.country = entity
    client.group = pay_group(cid, client.entity, "PG-IN")
    return client


def _contiguous(periods):
    for earlier, later in zip(periods, periods[1:]):
        assert date.fromisoformat(later["start"]) == date.fromisoformat(earlier["end"]) + timedelta(days=1), (earlier,
                                                                                                             later)
        assert date.fromisoformat(later["attendance_start"]) == \
            date.fromisoformat(earlier["attendance_end"]) + timedelta(days=1)


def test_ec_cal_11_monthly_calendar_generates_18_months_of_contiguous_open_periods(acme):
    result = calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    calendar = result["calendar"]
    assert calendar["frequency"] == "MONTHLY" and calendar["status"] == "ACTIVE"
    assert calendar["rules"]["monthly_start_day"] == 1 and calendar["rules"]["tax_year_start"]
    periods = periods_of(acme, acme.group)
    assert result["periods"] == len(periods) >= 18
    today = today_of(acme.company_id)
    assert periods[0]["start"] <= today.isoformat() <= periods[0]["end"]
    assert date.fromisoformat(periods[-1]["end"]) >= today + timedelta(days=540)
    assert all(p["status"] == "OPEN" and p["frequency"] == "MONTHLY" for p in periods)
    _contiguous(periods)
    assert all(p["input_cutoff"] < p["approval_deadline"] <= p["pay_date"] for p in periods)
    assert "PAY_CALENDAR_SAVED" in audit_actions(acme.company_id, "pay_calendar", calendar["id"])
    again = calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"},
                         effective_from=today.isoformat())
    assert again["calendar"]["id"] == calendar["id"] and again["periods"] == len(periods)  # unchanged: no-op


@pytest.mark.parametrize("frequency, rules, per_year", [
    ("SEMI_MONTHLY", {"semi_monthly_split_day": 15}, 24),
    ("WEEKLY", {"week_start_day": "MON"}, 52),
    ("DAILY", {}, 365),
])
def test_ec_cal_11_other_frequencies(acme, frequency, rules, per_year):
    calendar_for(acme, acme.group, frequency=frequency, rules=rules)
    periods = periods_of(acme, acme.group)
    _contiguous(periods)
    first_year = [p for p in periods if p["start"] < (date.fromisoformat(periods[0]["start"]) +
                                                      timedelta(days=365)).isoformat()]
    assert per_year - 1 <= len(first_year) <= per_year + 1
    # The pay group follows the calendar in force (wage inputs derive natural periods from it).
    frequency_now, rules_now = sql("SELECT pay_frequency, calendar_rules FROM pay_groups WHERE id = :g", g=acme.group)[0]
    assert frequency_now == frequency and all(rules_now.get(k) == v for k, v in rules.items())


def test_bi_weekly_needs_an_anchor_on_the_week_start_day(acme):
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "BI_WEEKLY", "rules": {}})
    assert r.status_code == 400 and r.json()["code"] == "BIWEEKLY_ANCHOR_REQUIRED", r.text
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar",
                  json={"frequency": "BI_WEEKLY", "rules": {"biweekly_anchor_date": "2026-01-06",
                                                            "week_start_day": "MON"}})
    assert r.status_code == 400 and r.json()["code"] == "BIWEEKLY_ANCHOR_WEEKDAY", r.text
    calendar_for(acme, acme.group, frequency="BI_WEEKLY",
                 rules={"biweekly_anchor_date": "2026-01-05", "week_start_day": "MON"})
    assert all(p["days"] == 14 for p in periods_of(acme, acme.group))


def test_ec_cal_01_start_day_31_clamps_to_the_month_end(acme):
    calendar_for(acme, acme.group, rules={"monthly_start_day": 31, "pay_day_rule": "LAST_DAY"})
    starts = {p["start"] for p in periods_of(acme, acme.group)}
    year = today_of(acme.company_id).year + 1
    feb_end = date(year, 3, 1) - timedelta(days=1)
    assert feb_end.isoformat() in starts  # 28 or 29 Feb
    assert date(year, 4, 30).isoformat() in starts


def test_unknown_rules_and_invalid_values_are_refused(acme):
    for rules, code in (({"surprise": 1}, "INVALID_RULES"), ({"monthly_start_day": 40}, "INVALID_MONTHLY_START_DAY"),
                        ({"pay_day_rule": "DAY_OF_MONTH"}, "PAY_DAY_VALUE_REQUIRED")):
        r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "MONTHLY", "rules": rules})
        assert r.status_code == 400 and r.json()["code"] == code, (rules, r.text)
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "FORTNIGHTLY", "rules": {}})
    assert r.status_code == 422
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "MONTHLY", "company_id": 1})
    assert r.status_code == 422
    assert periods_of(acme, acme.group) == []


def test_validate_only_previews_without_writing(acme):
    preview = ok(acme.post(f"{PAY}/pay-groups/{acme.group}/calendar",
                           json={"frequency": "MONTHLY", "rules": {"monthly_start_day": 1}, "validate_only": True}))
    assert preview["periods"] and preview["change"] is False
    assert periods_of(acme, acme.group) == []
    assert ok(acme.get(f"{PAY}/pay-groups/{acme.group}/calendar"))["current"] is None


def _national_calendar(company_id, country):
    return sql("INSERT INTO holiday_calendars (company_id, code, name, country_code, status, created_at, updated_at) "
               "VALUES (:c, 'BANK', 'Bank holidays', :cc, 'ACTIVE', now(), now()) RETURNING id",
               c=company_id, cc=country)[0][0]


def _holiday(company_id, calendar_id, day, name="Bank holiday"):
    sql("INSERT INTO holidays (company_id, name, date, calendar_id, holiday_type, is_optional, created_at, updated_at) "
        "VALUES (:c, :n, :d, :cal, 'PUBLIC', false, now(), now())", c=company_id, n=name, d=day, cal=calendar_id)


def _business_month_end(start_month: date) -> date:
    """The last day of a month (at least two months ahead) that is a weekday other than Friday or Saturday,
    so a holiday on it moves the pay date to the previous business day under every common weekend."""
    month = start_month
    while True:
        nxt = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = nxt - timedelta(days=1)
        if end.weekday() in (0, 1, 2, 3) and (end - timedelta(days=1)).weekday() in (0, 1, 2):
            return end
        month = nxt


def test_ec_cal_03_pay_date_on_a_bank_holiday_is_shifted(acme):
    today = today_of(acme.company_id)
    pay_day = _business_month_end((today.replace(day=1) + timedelta(days=70)).replace(day=1))
    bank = _national_calendar(acme.company_id, acme.country)
    _holiday(acme.company_id, bank, pay_day)
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY",
                                          "pay_day_shift": "PREVIOUS_BUSINESS_DAY"})
    period = next(p for p in periods_of(acme, acme.group) if p["end"] == pay_day.isoformat())
    assert period["nominal_pay_date"] == pay_day.isoformat()
    assert period["pay_date"] == (pay_day - timedelta(days=1)).isoformat()


def test_ec_cal_08_bank_holiday_added_later_is_reported_at_recheck(acme):
    today = today_of(acme.company_id)
    pay_day = _business_month_end((today.replace(day=1) + timedelta(days=70)).replace(day=1))
    bank = _national_calendar(acme.company_id, acme.country)
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    period = next(p for p in periods_of(acme, acme.group) if p["end"] == pay_day.isoformat())
    assert period["pay_date"] == pay_day.isoformat()
    _holiday(acme.company_id, bank, pay_day, "New bank holiday")
    check = ok(acme.get(f"{PAY}/pay-periods/{period['id']}/pay-date-check"))
    assert check["changed"] and check["current"] == (pay_day - timedelta(days=1)).isoformat()
    assert "PAY_DATE_SHIFTED" in {i["code"] for i in check["issues"]}
    stored = next(p for p in periods_of(acme, acme.group) if p["id"] == period["id"])
    assert stored["pay_date"] == pay_day.isoformat()  # stored dates never move silently


def test_ec_cal_06_pay_date_before_the_period_end_warns(acme):
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "DAY_OF_MONTH",
                                          "pay_day_value": 25, "pay_day_shift": "NO_SHIFT"})
    period = periods_of(acme, acme.group)[1]
    assert [w["rule"] for w in period["warnings"]] == ["V-14"]


def test_ec_cal_07_frequency_change_leaves_no_gap_or_overlap(acme):
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1, "pay_day_rule": "LAST_DAY"})
    before = periods_of(acme, acme.group)
    change_on = date.fromisoformat(before[2]["start"]) + timedelta(days=10)
    result = calendar_for(acme, acme.group, frequency="WEEKLY", rules={"week_start_day": "MON"},
                          effective_from=change_on.isoformat())
    after = periods_of(acme, acme.group)
    _contiguous(after)
    closing = next(p for p in after if p["end"] == (change_on - timedelta(days=1)).isoformat())
    assert closing["is_transition"] and closing["frequency"] == "MONTHLY"
    assert next(p for p in after if p["start"] == change_on.isoformat())["frequency"] == "WEEKLY"
    assert after[:2] == before[:2]  # earlier periods untouched
    history = ok(acme.get(f"{PAY}/pay-groups/{acme.group}/calendar"))["history"]
    assert [c["status"] for c in history] == ["ENDED", "ACTIVE"]
    assert history[0]["effective_to"] == (change_on - timedelta(days=1)).isoformat()
    assert "PAY_CALENDAR_CHANGED" in audit_actions(acme.company_id, "pay_calendar", result["calendar"]["id"])


def test_ec_cal_07_change_inside_a_period_with_a_run_is_refused(app, acme):
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1})
    current = periods_of(acme, acme.group)[0]
    new_run(acme, acme.group, current["id"], expect=201)
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar",
                  json={"frequency": "WEEKLY", "rules": {"week_start_day": "MON"},
                        "effective_from": (date.fromisoformat(current["start"]) + timedelta(days=5)).isoformat()})
    assert r.status_code == 400 and r.json()["code"] == "CHANGE_IN_LOCKED_PERIOD", r.text
    r = acme.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "WEEKLY", "rules": {}})
    assert r.status_code == 400 and r.json()["code"] == "EFFECTIVE_FROM_REQUIRED", r.text


def test_ec_cal_12_database_refuses_overlapping_periods(acme):
    calendar_for(acme, acme.group, rules={"monthly_start_day": 1})
    first = periods_of(acme, acme.group)[0]
    calendar_id = sql("SELECT id FROM pay_calendars WHERE pay_group_id = :g", g=acme.group)[0][0]
    with tenant(acme.company_id) as conn:
        expect_error(conn, EXCLUSION,
                     "INSERT INTO pay_periods (company_id, pay_calendar_id, pay_group_id, frequency, period_start, "
                     "period_end, attendance_start, attendance_end, pay_date, nominal_pay_date, input_cutoff_on, "
                     "approval_deadline_on, payment_submit_by, tax_year, number_in_tax_year, status, created_at, "
                     "updated_at) VALUES (:c, :cal, :g, 'MONTHLY', :s, :e, :s, :e, :e, :e, :s, :s, :e, '2026', 1, "
                     "'OPEN', now(), now())",
                     c=acme.company_id, cal=calendar_id, g=acme.group,
                     s=date.fromisoformat(first["start"]) + timedelta(days=3),
                     e=date.fromisoformat(first["end"]) + timedelta(days=3))


def test_generation_job_extends_the_horizon_idempotently(acme):
    from app.payroll.services import calendars

    calendar_for(acme, acme.group, rules={"monthly_start_day": 1})
    count = len(periods_of(acme, acme.group))
    from tests.payroll.test_runs_lifecycle import session_for

    db = session_for(acme.company_id)
    try:
        last = date.fromisoformat(periods_of(acme, acme.group)[-1]["end"])
        created = calendars.generate_ahead(db, acme.company_id, until=last + timedelta(days=75))
        db.commit()
        assert created in (3, 4)  # the periods overlapping the next 75 days
        assert calendars.generate_ahead(db, acme.company_id, until=last + timedelta(days=75)) == 0
        db.commit()
    finally:
        db.close()
    periods = periods_of(acme, acme.group)
    assert len(periods) == count + created
    _contiguous(periods)
    jobs = sql("SELECT idempotency_key FROM jobs WHERE company_id = :c AND type = 'payroll.generate_periods'",
               c=acme.company_id)
    assert jobs and all(k.startswith("generate-periods:") for (k,) in jobs)
    # The same daily tick activates scheduled compensation (FOLLOW-UPS F-38), one job per date.
    activation = sql("SELECT idempotency_key, run_at::date FROM jobs WHERE company_id = :c AND "
                     "type = 'compensation.activate_scheduled'", c=acme.company_id)
    assert activation and all(k == f"activate-scheduled:{day.isoformat()}" for k, day in activation)


def test_calendar_permissions_and_isolation(app, acme):
    from tests.helpers import invite_and_activate

    calendar_for(acme, acme.group, rules={"monthly_start_day": 1})
    beta = signup_company(app, "Beta Industries")
    for method, path, body in (("get", f"/pay-groups/{acme.group}/calendar", None),
                               ("post", f"/pay-groups/{acme.group}/calendar",
                                {"frequency": "MONTHLY", "rules": {}, "validate_only": True}),
                               ("get", f"/pay-groups/{acme.group}/periods", None)):
        r = getattr(beta, method)(f"{PAY}{path}", **({"json": body} if body else {}))
        assert r.status_code == 404, (path, r.text)
    period = periods_of(acme, acme.group)[0]
    assert beta.get(f"{PAY}/pay-periods/{period['id']}/pay-date-check").status_code == 404
    worker = invite_and_activate(app, acme, create_employee(acme, "Wendy"), ["employee"])
    assert worker.get(f"{PAY}/pay-groups/{acme.group}/periods").status_code == 403
    r = worker.post(f"{PAY}/pay-groups/{acme.group}/calendar", json={"frequency": "MONTHLY", "rules": {}})
    assert r.status_code == 403
