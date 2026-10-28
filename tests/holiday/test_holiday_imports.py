"""Holiday imports: CSV (dates, rules) and ICS, dry run then commit (LH-M1 Goal 5; 04-holiday/02 §7.4, §9.1-§9.3).

LH-REC-21 (RRULE subset: mapping and rejections) and the Goal 5 reading of LH-REC-21 ("CSV import sanitises inputs,
validates date formats and reports row-level errors"), matching (§9.3), commit into DRAFT and PUBLISHED years,
the target-hash guard, limits, templates and errors.csv.
"""

from datetime import date

from tests.attendance_helpers import fake_clock, owner_rows  # noqa: F401  (fake_clock is a fixture)
from tests.holiday.test_day_facts_v2 import HC, _calendar, _published, alpha  # noqa: F401  (alpha is a fixture)

D = date


def _upload(client, calendar_id, content, kind="CSV_DATED", year=2027, name="holidays.csv"):
    return client.post(f"{HC}/{calendar_id}/imports", data={"kind": kind, "target_year": str(year)},
                       files={"file": (name, content.encode("utf-8") if isinstance(content, str) else content,
                                       "text/csv")})


def _by_line(report):
    return {ln["line"]: ln for ln in report["lines"] if ln["line"] is not None}


CSV = """date,name,days,type,category,portion,status,description
2027-01-26,Republic Day,1,PUBLIC,NATIONAL,FULL,CONFIRMED,
2027-08-15,  Independence   Day ,1,public,national,,,<b>Flag</b> hoisting
26/01/2027,Bad format,1,COMPANY,,,,
2026-12-31,Wrong year,1,COMPANY,,,,
2027-02-30,No such day,1,COMPANY,,,,
2027-03-01,=HYPERLINK("http://x"),1,COMPANY,,,,
2027-04-01,Half,1,COMPANY,,SECOND_HALF,,
2027-05-01,,1,COMPANY,,,,
2027-06-01,Typed wrong,1,HOLIDAY,,,,
"""


def test_lh_rec_21_csv_sanitises_validates_dates_and_reports_line_errors(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Import target", country_code="IN")
    r = _upload(admin, cal["id"], CSV)
    assert r.status_code == 201, r.text
    report = r.json()
    lines = _by_line(report)
    assert report["status"] == "DRY_RUN" and report["year_status"] == "NONE"
    assert lines[2]["classification"] == "NEW" and lines[2]["action"] == "ACCEPT"
    assert lines[3]["values"]["name"] == "Independence Day"  # whitespace collapsed
    assert lines[3]["values"]["holiday_type"] == "PUBLIC" and lines[3]["values"]["category"] == "NATIONAL"
    assert lines[3]["values"]["description"] == "Flag hoisting"  # markup removed
    assert "is not a YYYY-MM-DD date" in lines[4]["errors"][0]["message"] and lines[4]["classification"] == "ERROR"
    assert lines[5]["errors"][0]["message"] == "Date must be in 2027."
    assert "not a calendar date" in lines[6]["errors"][0]["message"]
    assert lines[7]["values"]["name"].startswith("=HYPERLINK") and lines[7]["classification"] == "NEW"  # text only
    assert lines[8]["errors"][0]["code"] == "INVALID_PORTION"
    assert lines[9]["errors"][0] == {"field": "name", "message": "Required."}
    assert lines[10]["errors"][0]["field"] == "type"
    assert report["summary"]["ERROR"] == 6 and report["summary"]["NEW"] == 3
    assert all(ln["action"] == "SKIP" for ln in lines.values() if ln["classification"] == "ERROR")
    errors = admin.get(f"{HC}/imports/{report['id']}/errors.csv")
    assert errors.status_code == 200 and errors.text.startswith("line,field,message")
    assert "Date must be in 2027." in errors.text
    # Nothing but the report was written.
    assert not owner_rows("SELECT id FROM holidays WHERE calendar_id = :k", k=cal["id"])


def test_csv_commit_into_a_draft_year_and_the_import_cannot_run_twice(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Draft import", country_code="IN")
    report = _upload(admin, cal["id"], CSV).json()
    skip_formula = [{"line": 7, "action": "SKIP"}]
    done = admin.post(f"{HC}/{cal['id']}/imports/{report['id']}/commit", json={"decisions": skip_formula})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "COMMITTED" and done.json()["result"]["rows_created"] == 2
    rows = owner_rows("SELECT name, date, source, source_ref, holiday_type FROM holidays WHERE calendar_id = :k "
                      "ORDER BY date", k=cal["id"])
    assert [r["name"] for r in rows] == ["Republic Day", "Independence Day"]
    assert all(r["source"] == "IMPORT" and r["source_ref"]["import_id"] == report["id"] for r in rows)
    year = admin.get(f"{HC}/{cal['id']}/years/2027").json()
    assert year["status"] == "DRAFT"
    twice = admin.post(f"{HC}/imports/{report['id']}/commit", json={})
    assert (twice.status_code, twice.json()["code"]) == (409, "IMPORT_NOT_OPEN")
    # Re-import the same file: the two committed lines are now UNCHANGED, nothing new by default.
    again = _upload(admin, cal["id"], CSV).json()
    lines = _by_line(again)
    assert lines[2]["classification"] == "UNCHANGED" and lines[3]["classification"] == "UNCHANGED"
    audit = owner_rows("SELECT action FROM audit_logs WHERE company_id = :c AND action IN ('IMPORT_DRY_RUN', "
                       "'IMPORT_COMMITTED')", c=alpha["company_id"])
    assert sorted(a["action"] for a in audit) == ["IMPORT_COMMITTED", "IMPORT_DRY_RUN", "IMPORT_DRY_RUN"]


def test_matching_moves_conflicts_and_the_target_hash_guard(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Match target", country_code="IN")
    for name, day in (("Founders Day", "2027-03-10"), ("Harvest", "2027-04-14")):
        assert admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": name, "date": day,
                                                                "holiday_type": "COMPANY"}).status_code == 201
    csv_text = ("date,name,category\n2027-03-12,founders day,OTHER\n2027-04-14,Harvest Fair,OTHER\n"
                "2027-05-05,Labour Day,OTHER\n")
    report = _upload(admin, cal["id"], csv_text).json()
    lines = _by_line(report)
    # MANUAL rows are tenant edits: a same-name row two days away is a conflict with your edit, default Skip
    assert lines[2]["classification"] == "CONFLICT_EDIT" and lines[2]["action"] == "SKIP"
    assert lines[3]["classification"] == "CONFLICT" and "Possible duplicate" in lines[3]["note"]
    assert lines[4]["classification"] == "NEW"
    # The calendar changes after the check: the commit is refused.
    assert admin.post(f"{HC}/{cal['id']}/occurrences", json={"name": "Extra", "date": "2027-06-06",
                                                            "holiday_type": "COMPANY"}).status_code == 201
    stale = admin.post(f"{HC}/imports/{report['id']}/commit", json={})
    assert (stale.status_code, stale.json()["code"]) == (409, "ROW_VERSION_CONFLICT")


def test_import_into_a_published_year_is_one_amendment_with_a_reason(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Published target", country_code="IN")
    _published(admin, cal["id"], [("Republic Day", "2027-01-26", "PUBLIC")], year=2027)
    report = _upload(admin, cal["id"], "date,name,type\n2027-11-01,State Day,PUBLIC\n").json()
    assert report["year_status"] == "PUBLISHED"
    no_reason = admin.post(f"{HC}/imports/{report['id']}/commit", json={})
    assert (no_reason.status_code, no_reason.json()["code"]) == (422, "REASON_REQUIRED")
    done = admin.post(f"{HC}/imports/{report['id']}/commit", json={"reason": "State notification"})
    assert done.status_code == 200, done.text
    assert done.json()["amendment"]["status"] == "AMENDED"
    assert admin.get(f"{HC}/{cal['id']}/years/2027").json()["version"] == 2
    row = owner_rows("SELECT source FROM holidays WHERE calendar_id = :k AND name = 'State Day'", k=cal["id"])[0]
    assert row["source"] == "IMPORT"


ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
X-WR-CALNAME:Gamma US\r
BEGIN:VEVENT\r
UID:thanks-2027@example\r
DTSTART;VALUE=DATE:20271125\r
DTEND;VALUE=DATE:20271127\r
SUMMARY:Thanksgiving\r
  Break\r
CATEGORIES:NATIONAL\r
TRANSP:TRANSPARENT\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:eid-2027@example\r
DTSTART;VALUE=DATE:20270309\r
SUMMARY:Eid al-Fitr\r
STATUS:TENTATIVE\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:meeting@example\r
DTSTART:20270310T090000Z\r
SUMMARY:Town hall\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:june-1st-monday@example\r
DTSTART;VALUE=DATE:20270607\r
RRULE:FREQ=YEARLY;BYMONTH=6;BYDAY=1MO\r
SUMMARY:June Monday\r
BEGIN:VALARM\r
ACTION:DISPLAY\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:sat-off@example\r
DTSTART;VALUE=DATE:20270109\r
RRULE:FREQ=MONTHLY;BYDAY=2SA,4SA\r
SUMMARY:Saturdays off\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_lh_rec_21_ics_events_rrule_subset_and_rejections(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Gamma ICS", country_code="US")
    r = _upload(admin, cal["id"], ICS, kind="ICS", name="us.ics")
    assert r.status_code == 201, r.text
    report = r.json()
    lines = _by_line(report)
    assert report["source_label"] == "Gamma US"
    assert {"TRANSP", "VALARM"} <= set(report["ignored_properties"])
    thanks = lines[1]
    assert thanks["values"]["name"] == "Thanksgiving Break" and thanks["values"]["days"] == 2  # DTEND exclusive
    assert thanks["values"]["category"] == "NATIONAL" and thanks["classification"] == "NEW"
    assert lines[2]["values"]["status"] == "PROVISIONAL"  # TENTATIVE
    assert "Timed events are not holidays" in lines[3]["errors"][0]["message"]
    june = lines[4]
    assert june["kind"] == "RULE" and june["values"]["kind"] == "NTH_WEEKDAY"
    assert june["classification"] == "NEW_RULE" and june["preview"][0]["date"] == "2027-06-07"
    sat = lines[5]
    assert sat["classification"] == "ERROR" and sat["errors"][0]["reason"] == "RRULE_UNSUPPORTED"
    done = admin.post(f"{HC}/imports/{report['id']}/commit", json={})
    assert done.status_code == 200, done.text
    assert done.json()["result"]["rules_created"] == 1 and done.json()["result"]["rows_created"] >= 2
    rules = admin.get(f"{HC}/{cal['id']}/rules").json()
    assert any(rule["name"] == "June Monday" and rule["kind"] == "NTH_WEEKDAY" for rule in rules["items"])
    uid = owner_rows("SELECT source_ref FROM holidays WHERE calendar_id = :k AND name = 'Thanksgiving Break' AND "
                     "date = '2027-11-25'", k=cal["id"])[0]["source_ref"]
    assert uid["external_uid"] == "thanks-2027@example" and uid["source_id"] == "ics:Gamma US"
    # Re-import: matched by UID, nothing changes; a moved event is a date change.
    moved = ICS.replace("DTSTART;VALUE=DATE:20270309", "DTSTART;VALUE=DATE:20270310")
    again = _by_line(_upload(admin, cal["id"], moved, kind="ICS", name="us.ics").json())
    assert again[1]["classification"] == "UNCHANGED"
    assert again[2]["classification"] == "CHANGED_DATE" and again[2]["action"] == "ACCEPT"


def test_csv_rules_template_and_file_limits(alpha):
    admin = alpha["admin"]
    cal = _calendar(admin, "Rules import", country_code="US")
    template = admin.get(f"{HC}/imports/templates/CSV_RULES", params={"year": 2027})
    assert template.status_code == 200 and template.text.startswith("name,kind,month")
    report = _upload(admin, cal["id"], template.text, kind="CSV_RULES").json()
    lines = _by_line(report)
    assert all(ln["classification"] == "NEW_RULE" for ln in lines.values()), [ln["errors"] for ln in lines.values()]
    thanksgiving = next(ln for ln in lines.values() if ln["values"]["name"] == "Thanksgiving")
    assert thanksgiving["preview"][0]["date"] == "2027-11-25"
    good_friday = next(ln for ln in lines.values() if ln["values"]["name"] == "Good Friday")
    assert good_friday["preview"][0]["date"] == "2027-03-26"
    done = admin.post(f"{HC}/imports/{report['id']}/commit", json={})
    assert done.status_code == 200 and done.json()["result"]["rules_created"] == 3
    dated = admin.get(f"{HC}/imports/templates/CSV_DATED").text
    assert dated.startswith("date,name,days")
    missing = _upload(admin, cal["id"], "name,when\nX,2027-01-01\n")
    assert (missing.status_code, missing.json()["code"]) == (422, "IMPORT_INVALID_FILE")
    huge = _upload(admin, cal["id"], "date,name\n" + "2027-01-01,x\n" * 200_000)
    assert (huge.status_code, huge.json()["code"]) == (422, "IMPORT_TOO_LARGE")
    not_ics = _upload(admin, cal["id"], "hello", kind="ICS", name="x.ics")
    assert (not_ics.status_code, not_ics.json()["code"]) == (422, "IMPORT_INVALID_FILE")
    assert alpha["worker_client"].post(f"{HC}/{cal['id']}/imports", data={"kind": "CSV_DATED", "target_year": "2027"},
                                       files={"file": ("a.csv", b"date,name\n", "text/csv")}).status_code == 403
    listed = admin.get(f"{HC}/{cal['id']}/imports").json()["items"]
    assert listed and listed[0]["status"] == "COMMITTED"


def test_white_label_tenants_never_see_or_touch_each_others_imports_rollover_or_closures(alpha):
    """Multi-company SaaS (RLS + scope): a second tenant's administrator gets 404 for another tenant's calendar,
    import report, errors file, commit, closure and calendar days; its own Next year page lists only its own
    calendars; each tenant keeps its own rollover settings."""
    from tests.helpers import signup_company

    admin = alpha["admin"]
    cal = _calendar(admin, "Alpha only", country_code="IN")
    report = _upload(admin, cal["id"], "date,name\n2027-03-03,Alpha Day\n").json()
    other = signup_company(alpha["app"], "Beta Tenant")
    assert other.get(f"{HC}/{cal['id']}").status_code == 404
    assert other.get(f"{HC}/imports/{report['id']}").status_code == 404
    assert other.get(f"{HC}/imports/{report['id']}/errors.csv").status_code == 404
    assert other.post(f"{HC}/imports/{report['id']}/commit", json={}).status_code == 404
    assert other.post(f"{HC}/{cal['id']}/imports", data={"kind": "CSV_DATED", "target_year": "2027"},
                      files={"file": ("a.csv", b"date,name\n2027-03-04,X\n", "text/csv")}).status_code == 404
    assert other.post(f"{HC}/{cal['id']}/closures", json={"date": "2026-11-02", "reason": "x",
                                                          "dry_run": True}).status_code == 404
    assert other.get("/api/v1/calendar/days", params={"employee_id": alpha["worker"]["id"], "from": "2026-11-01",
                                                      "to": "2026-11-02"}).status_code == 404
    mine = other.get(f"{HC}/rollover").json()
    assert all(item["calendar_id"] != cal["id"] for item in mine["items"])
    # The report is still open for its own tenant, untouched by the other tenant's attempts.
    assert admin.get(f"{HC}/imports/{report['id']}").json()["status"] == "DRY_RUN"
    assert admin.post(f"{HC}/imports/{report['id']}/commit", json={}).status_code == 200
    assert not owner_rows("SELECT id FROM holidays WHERE company_id = :c AND name IN ('Alpha Day', 'X')",
                          c=other.company_id)
