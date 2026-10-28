"""Year generation from holiday rules (LH 04-holiday/02 §4.4, §8; LH 14 WI-1.8).

``generate`` writes a DRAFT year from ``holiday_recurrence.generate_year``: it builds calendar year Y from rule-years
Y-1..Y+1, respects the rows it must not rewrite (manual rows, overridden or skipped rule rows, rows of PUBLISHED years,
included calendars' published rows) and reconciles its output with the stored rule rows of the DRAFT year by
(``rule_id``, ``occurrence_key``) — insert / update in place / delete — so running it twice with the same inputs
changes nothing (LH-REC-14). A PUBLISHED year is never written here: ``published_difference`` tells the caller what a
rule change would add or change there, for an amendment (§4.6, LH-REC-03).

Expected start dates of ANNUAL_CONFIRM rules (§6.2): TENANT dates live in ``holiday_rules.source_ref.expected_dates``
(``{"2027": "2027-03-09"}``, DECISIONS IG-LH-17); pack, import and connector estimates arrive with those features.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core import clock
from app.core.principal import Principal
from app.models.holiday import Holiday, HolidayCalendarYear, HolidayRule
from app.models.pay_org import HolidayCalendar
from app.services import holiday_recurrence as rec
from app.services.audit_log_service import AuditLogService

# Fields of a generated row compared during reconciliation (row identity is the occurrence key).
_COMPARED = ("date", "name", "holiday_type", "category", "portion", "status", "observance", "observed_from_date",
             "sequence_index")


def rule_spec(row: HolidayRule) -> rec.RuleSpec:
    """A stored rule as engine input (``applies_on`` NULL = the calendar's effective weekend)."""
    return rec.RuleSpec(
        kind=row.kind, params=dict(row.params or {}), name=row.name, id=row.id, holiday_type=row.holiday_type,
        category=row.category, portion=row.portion, duration_days=int(row.duration_days or 1),
        duration_range=tuple(row.duration_range) if row.duration_range else None, observed_rule=row.observed_rule,
        applies_on=frozenset(row.applies_on) if row.applies_on else None, valid_from_year=int(row.valid_from_year),
        valid_to_year=int(row.valid_to_year) if row.valid_to_year is not None else None, status=row.status,
        version=int(row.version or 1), name_i18n=dict(row.name_i18n or {}),
    )


def as_occurrence(h: Holiday) -> rec.Occurrence:
    ref = h.source_ref or {}
    return rec.Occurrence(
        date=h.date, name=h.name, holiday_type=h.holiday_type or "COMPANY", category=h.category or "OTHER",
        portion=h.portion or "FULL", status=h.status or "CONFIRMED", observance=h.observance or "ORIGINAL",
        observed_from_date=h.observed_from_date, sequence_index=int(h.sequence_index or 1), rule_id=h.rule_id,
        rule_year=ref.get("rule_year"), rule_version=ref.get("rule_version"), occurrence_key=ref.get("occurrence_key"),
    )


def rules_of(db: Session, company_id: int, calendar_id: int) -> List[HolidayRule]:
    return (db.query(HolidayRule).filter(HolidayRule.company_id == company_id, HolidayRule.calendar_id == calendar_id)
            .order_by(HolidayRule.id).all())


def year_record(db: Session, company_id: int, calendar_id: int, year: int) -> Optional[HolidayCalendarYear]:
    return db.query(HolidayCalendarYear).filter(
        HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id == calendar_id,
        HolidayCalendarYear.year == year).first()


def expected_dates(rules: Iterable[HolidayRule]) -> Dict[Tuple[int, int], date]:
    """``(rule_id, rule_year) -> expected start`` of ANNUAL_CONFIRM rules, TENANT source (IG-LH-17)."""
    out: Dict[Tuple[int, int], date] = {}
    for rule in rules:
        if rule.kind != "ANNUAL_CONFIRM":
            continue
        for year, value in ((rule.source_ref or {}).get("expected_dates") or {}).items():
            picked = rec.select_expected_start({"TENANT": date.fromisoformat(value)})
            if picked:
                out[(rule.id, int(year))] = picked[1]
    return out


def _own_rows(db: Session, company_id: int, calendar_id: int, first: int, last: int) -> List[Holiday]:
    return db.query(Holiday).filter(
        Holiday.company_id == company_id, Holiday.calendar_id == calendar_id,
        Holiday.date >= date(first, 1, 1), Holiday.date <= date(last, 12, 31)).order_by(Holiday.date, Holiday.id).all()


def published_years(db: Session, company_id: int, calendar_id: int, years: Iterable[int]) -> set:
    wanted = sorted(set(years))
    published = {y for (y,) in db.query(HolidayCalendarYear.year).filter(
        HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id == calendar_id,
        HolidayCalendarYear.year.in_(wanted), HolidayCalendarYear.status == "PUBLISHED")}
    drafts = {y for (y,) in db.query(HolidayCalendarYear.year).filter(
        HolidayCalendarYear.company_id == company_id, HolidayCalendarYear.calendar_id == calendar_id,
        HolidayCalendarYear.year.in_(wanted), HolidayCalendarYear.status == "DRAFT")}
    # A year with rows but no record is a legacy (published) year (IG-LH-15).
    legacy = {y for y in wanted if y not in published and y not in drafts and db.query(Holiday.id).filter(
        Holiday.company_id == company_id, Holiday.calendar_id == calendar_id,
        Holiday.date >= date(y, 1, 1), Holiday.date <= date(y, 12, 31)).first()}
    return published | legacy


def _inputs(db: Session, company_id: int, calendar: HolidayCalendar, year: int):
    """Engine inputs for calendar year ``year``: rules, weekend, fixed rows and expected dates (§4.3).

    Every non-cancelled own row of Y-1..Y+1 is a fixed (taken) row; a stored rule row carries its occurrence key, so
    it never blocks its own regeneration (the engine's owner rule), while manual rows, overridden rows and rows of
    PUBLISHED years keep their dates. Included calendars contribute their published rows after this calendar's
    exclusions (§4.6 last paragraph).
    """
    from app.services import holiday_calendar_service as cal_service

    rules = rules_of(db, company_id, calendar.id)
    weekend = frozenset(cal_service.effective_weekend(db, calendar))
    fixed = [as_occurrence(h) for h in _own_rows(db, company_id, calendar.id, year - 1, year + 1)
             if h.status != "CANCELLED"]
    flat = cal_service.Flattener(db, company_id).flatten(calendar.id, date(year - 1, 1, 1), date(year + 1, 12, 31))
    inherited = [f.row.id for f in flat.rows if f.inherited]
    if inherited:
        fixed.extend(as_occurrence(h) for h in db.query(Holiday).filter(Holiday.company_id == company_id,
                                                                      Holiday.id.in_(inherited)))
    return rules, weekend, fixed


def run_generator(db: Session, company_id: int, calendar: HolidayCalendar, year: int) -> rec.GenerationResult:
    rules, weekend, fixed = _inputs(db, company_id, calendar, year)
    specs = [rule_spec(r) for r in rules]
    return rec.generate_year(specs, year, weekend, fixed, expected_dates(rules))


def _issue_json(issue: rec.Issue) -> dict:
    return {"code": issue.code, "message": issue.message, "severity": issue.severity, "rule_id": issue.rule_id,
            "year": issue.year}


def occurrence_json(o: rec.Occurrence) -> dict:
    return {"date": o.date.isoformat(), "weekday": o.date.weekday(), "name": o.name, "holiday_type": o.holiday_type,
            "category": o.category, "portion": o.portion, "status": o.status, "observance": o.observance,
            "observed_from_date": o.observed_from_date.isoformat() if o.observed_from_date else None,
            "sequence_index": o.sequence_index, "rule_id": o.rule_id, "rule_year": o.rule_year,
            "occurrence_key": o.occurrence_key}


def _values(o: rec.Occurrence) -> Dict[str, Any]:
    return {"date": o.date, "name": o.name, "holiday_type": o.holiday_type, "category": o.category,
            "portion": o.portion, "status": o.status, "observance": o.observance,
            "observed_from_date": o.observed_from_date, "sequence_index": o.sequence_index}


def published_difference(db: Session, company_id: int, calendar: HolidayCalendar, year: int) -> List[dict]:
    """What the rules would add to or change in a PUBLISHED year (never written here; §4.6, LH-REC-03)."""
    result = run_generator(db, company_id, calendar, year)
    stored = {(h.rule_id, (h.source_ref or {}).get("occurrence_key")): h
              for h in _own_rows(db, company_id, calendar.id, year, year) if h.rule_id is not None}
    out = []
    for o in result.rows:
        h = stored.get((o.rule_id, o.occurrence_key))
        if h is None:
            out.append({"change": "add", **occurrence_json(o)})
        elif not (h.source_ref or {}).get("overridden") and h.status != "CANCELLED" and any(
                getattr(h, k) != v for k, v in _values(o).items() if k != "status"):
            out.append({"change": "update", "holiday_id": h.id, **occurrence_json(o)})
    for o in result.cross_year_items:
        out.append({"change": "cross_year", **occurrence_json(o)})
    return out


def generate(db: Session, company_id: int, calendar_id: int, year: int, actor: Optional[Principal] = None) -> dict:
    """Write the DRAFT year from the rules (§4.4); the caller has checked permission and that the year is a DRAFT."""
    from app.services import holiday_years

    calendar = db.get(HolidayCalendar, calendar_id)
    record = holiday_years.ensure_year(db, company_id, calendar, year, lock=True)
    if record.status == "PUBLISHED":
        from app.core.errors import conflict

        raise conflict(f"{year} is published; changes go through an amendment.", "YEAR_PUBLISHED")
    result = run_generator(db, company_id, calendar, year)
    stored: Dict[Tuple[int, str], Holiday] = {}
    for h in _own_rows(db, company_id, calendar.id, year, year):
        if h.rule_id is not None:
            stored[(h.rule_id, (h.source_ref or {}).get("occurrence_key"))] = h
    counts = defaultdict(int)
    notes = [_issue_json(n) for n in result.notes]
    rule_versions = {r.id: r.version for r in rules_of(db, company_id, calendar.id)}
    seen = set()
    for o in result.rows:
        key = (o.rule_id, o.occurrence_key)
        seen.add(key)
        h = stored.get(key)
        ref = {"rule_version": rule_versions.get(o.rule_id, o.rule_version), "rule_year": o.rule_year,
               "occurrence_key": o.occurrence_key}
        if o.status == "PROVISIONAL":
            ref["expected_source"] = "TENANT"
        if h is None:
            clash = db.query(Holiday.id).filter(
                Holiday.company_id == company_id, Holiday.calendar_id == calendar.id, Holiday.date == o.date,
                Holiday.status != "CANCELLED", Holiday.name.ilike(o.name)).first()
            if clash:
                notes.append({"code": "DUPLICATE_HOLIDAY", "severity": "info", "rule_id": o.rule_id, "year": year,
                              "message": f"{o.name} on {o.date.isoformat()} is already on the calendar"})
                continue
            db.add(Holiday(company_id=company_id, calendar_id=calendar.id, rule_id=o.rule_id, source="RULE",
                           source_ref=ref, is_optional=o.holiday_type == "OPTIONAL", location=None,
                           name_i18n=dict(o.name_i18n or {}), **_values(o)))
            counts["inserted"] += 1
            continue
        existing_ref = dict(h.source_ref or {})
        if h.status == "CANCELLED":
            counts["kept_skipped"] += 1
            notes.append({"code": "SKIPPED_THIS_YEAR", "severity": "info", "rule_id": o.rule_id, "year": year,
                          "message": f"{o.name}: skipped this year"})
            continue
        if existing_ref.get("overridden"):
            counts["kept_overridden"] += 1
            if h.date != o.date:
                notes.append({"code": "OVERRIDDEN", "severity": "info", "rule_id": o.rule_id, "year": year,
                              "message": f"Rule gives {o.date.isoformat()}; you changed it to {h.date.isoformat()}"})
            continue
        values = _values(o)
        if any(getattr(h, k) != v for k, v in values.items()) or existing_ref != {**existing_ref, **ref}:
            for k, v in values.items():
                setattr(h, k, v)
            h.is_optional = o.holiday_type == "OPTIONAL"
            h.source_ref = {**existing_ref, **ref}
            h.row_version = (h.row_version or 1) + 1
            counts["updated"] += 1
    for key, h in stored.items():
        if key not in seen and not (h.source_ref or {}).get("overridden"):
            db.delete(h)
            counts["deleted"] += 1
    counts["date_needed"] = sum(1 for n in result.notes if n.code == "HOLIDAY_DATE_NEEDED")
    db.flush()
    holiday_years.touch_draft(db, record)
    record.generated_at = clock.utcnow()
    AuditLogService.log(db, actor, "Holidays", "YEAR_GENERATED", f"{calendar.code} {year}: generated from rules",
                        "holiday_calendar", calendar.id,
                        details={"year": year, "counts": dict(counts), "rule_versions": rule_versions})
    return {"calendar_id": calendar.id, "year": year, "counts": {k: counts.get(k, 0) for k in (
        "inserted", "updated", "deleted", "kept_overridden", "kept_skipped", "date_needed")},
        "notes": notes, "cross_year_items": [occurrence_json(o) for o in result.cross_year_items]}


def diff(rows_y: Sequence[Mapping[str, Any]], rows_prev: Sequence[Mapping[str, Any]]) -> List[dict]:
    """Next-year review lines (§8.6): match by rule and sequence, else by name; NEW / SAME / MOVED / GONE."""
    def key(r):
        return ("R", r.get("rule_id"), r.get("sequence_index"), r.get("observance")) if r.get("rule_id") \
            else ("N", (r.get("name") or "").strip().lower())

    prev = {key(r): r for r in rows_prev if r.get("status") != "CANCELLED"}
    out, matched = [], set()
    for r in rows_y:
        if r.get("status") == "CANCELLED":
            continue
        k = key(r)
        p = prev.get(k)
        if p is None:
            out.append({"line": "NEW", "name": r["name"], "date": r["date"], "previous_date": None})
            continue
        matched.add(k)
        same_day = p["date"][5:] == r["date"][5:]
        out.append({"line": "SAME" if same_day else "MOVED", "name": r["name"], "date": r["date"],
                    "previous_date": p["date"]})
    for k, p in prev.items():
        if k not in matched:
            out.append({"line": "GONE", "name": p["name"], "date": None, "previous_date": p["date"]})
    return sorted(out, key=lambda x: (x["date"] or x["previous_date"] or "", x["name"]))


# ============================================================================================== rollover (§8)
ROLLOVER_JOB = "holiday.rollover"
REMINDERS_JOB = "holiday.reminders"
UNPUBLISHED_REMINDERS = ((12, 1), (12, 15), (1, 1))  # §8.6, SPINE V3-104 (fixed in v2)
NEXT_YEAR_LINK = "/holidays/rollover?year={year}"


def _setting(db: Session, company_id: int, key: str, default: Any) -> Any:
    from app.services import holiday_years

    return holiday_years._setting(db, company_id, key, default)  # noqa: SLF001


def generate_on(db: Session, company_id: int, year: int) -> date:
    """``holiday.rollover.generate_on`` (MM-DD, default 10-01) in ``year``; 02-29 falls back to 02-28."""
    raw = str(_setting(db, company_id, "holiday.rollover.generate_on", "10-01") or "10-01")
    try:
        month, day = (int(x) for x in raw.split("-"))
        return date(year, month, min(day, 28) if month == 2 else day)
    except (TypeError, ValueError):
        return date(year, 10, 1)


def active_rules_in(rules: Iterable[HolidayRule], year: int) -> List[HolidayRule]:
    return [r for r in rules if r.status == "ACTIVE" and (r.valid_from_year or 0) <= year
            and (r.valid_to_year is None or r.valid_to_year >= year)]


def topological(db: Session, company_id: int, calendars: Sequence[HolidayCalendar]) -> List[HolidayCalendar]:
    """Included calendars first, then by id (§8.3 step 1): a dependent's observed chains see its includes' drafts."""
    from app.services.holiday_calendar_service import _graph  # noqa: PLC2701

    graph = _graph(db, company_id)
    by_id = {c.id: c for c in calendars}
    out: List[HolidayCalendar] = []
    seen: set = set()

    def visit(node: int, depth: int = 0) -> None:
        if node in seen or depth > 64:
            return
        seen.add(node)
        for _, child in sorted(graph.get(node, ()), key=lambda x: x[1]):
            visit(child, depth + 1)
        if node in by_id:
            out.append(by_id[node])

    for cal in sorted(calendars, key=lambda c: c.id):
        visit(cal.id)
    return out


def summarise(db: Session, company_id: int, calendar: HolidayCalendar, year: int,
              generated: Optional[Mapping[str, Any]] = None) -> Dict[str, int]:
    """Counts of the Next year page (§8.5): holidays, needs a date, on a weekend, observed, edited, skipped,
    expected (PROVISIONAL) and cross-year items of the last generation."""
    from app.services import holiday_calendar_service as svc

    weekend = set(svc.effective_weekend(db, calendar))
    rows = _own_rows(db, company_id, calendar.id, year, year)
    live = [h for h in rows if h.status != "CANCELLED"]
    previous = (getattr(year_record(db, company_id, calendar.id, year), "generation_summary", None) or {})
    counts = {
        "holidays": len(live),
        "expected": sum(1 for h in live if h.status == "PROVISIONAL"),
        "weekend": sum(1 for h in live if h.date.weekday() in weekend),
        "observed": sum(1 for h in live if h.observance == "OBSERVED"),
        "edited": sum(1 for h in live if (h.source_ref or {}).get("overridden")),
        "skipped": sum(1 for h in rows if h.status == "CANCELLED" and h.rule_id is not None),
        "needs_date": int(previous.get("needs_date", 0)),
        "cross_year": int(previous.get("cross_year", 0)),
    }
    if generated is not None:
        counts["needs_date"] = int(generated.get("counts", {}).get("date_needed", 0))
        counts["cross_year"] = len(generated.get("cross_year_items") or ())
    return counts


def run_rollover(db: Session, company_id: int, year: int, actor: Optional[Principal] = None, *,
                 calendar_ids: Optional[Iterable[int]] = None, only_missing: bool = False,
                 notify: bool = True) -> dict:
    """One rollover run of a tenant (§8.3): every ACTIVE calendar with ACTIVE rules valid in ``year`` and no PUBLISHED
    year gets its DRAFT generated (an existing DRAFT is reconciled, §4.4, so a rerun duplicates nothing,
    LH-REC-11/-14), included calendars first. A calendar that fails is marked FAILED and the others go on (LH-REC-30).
    Nothing is published (D-LH-09). Publishers get one "draft ready" notification per run."""
    from app.services import holiday_years

    query = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                             HolidayCalendar.status == "ACTIVE")
    if calendar_ids is not None:
        query = query.filter(HolidayCalendar.id.in_(sorted(set(calendar_ids)) or [0]))
    results: List[dict] = []
    totals = defaultdict(int)
    for calendar in topological(db, company_id, query.all()):
        line = {"calendar_id": calendar.id, "code": calendar.code}
        if not active_rules_in(rules_of(db, company_id, calendar.id), year):
            results.append({**line, "result": "NO_RULES"})
            continue
        state = holiday_years.state_of(db, company_id, calendar.id, year)
        if state == "PUBLISHED":
            results.append({**line, "result": "PUBLISHED"})
            continue
        record = year_record(db, company_id, calendar.id, year)
        if only_missing and record is not None and record.generation_status == "OK":
            results.append({**line, "result": "EXISTS"})
            continue
        savepoint = db.begin_nested()
        try:
            generated = generate(db, company_id, calendar.id, year, actor)
            record = year_record(db, company_id, calendar.id, year)
            summary = summarise(db, company_id, calendar, year, generated)
            record.generation_status, record.generation_error, record.generation_summary = "OK", None, summary
            db.flush()
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 - one calendar's failure never blocks the others (§8.3)
            savepoint.rollback()
            message = str(getattr(exc, "message", None) or getattr(exc, "detail", None) or exc)[:500] or "Error"
            failed = holiday_years.ensure_year(db, company_id, calendar, year, lock=True)
            if failed.status != "PUBLISHED":
                failed.generation_status, failed.generation_error = "FAILED", message
                db.flush()
            results.append({**line, "result": "FAILED", "error": message})
            totals["failed"] += 1
            continue
        results.append({**line, "result": "GENERATED", "counts": generated["counts"], "summary": summary})
        totals["calendars"] += 1
        for key in ("holidays", "needs_date", "weekend"):
            totals[key] += summary[key]
    if notify and totals["calendars"]:
        _notify_draft_ready(db, company_id, year, totals)
    if actor is None and totals["calendars"]:
        AuditLogService.log(db, None, "Holidays", "ROLLOVER_RUN", f"{year} drafts generated",
                            "company", company_id, details={"year": year, **dict(totals)})
    return {"year": year, "calendars": results, "totals": dict(totals)}


def _publisher_entities(db: Session, company_id: int) -> List[int]:
    from app.models.org import LegalEntity

    return [e for (e,) in db.query(LegalEntity.id).filter(LegalEntity.company_id == company_id).order_by(
        LegalEntity.id)]


def publisher_ids(db: Session, company_id: int, legal_entity_id: Optional[int] = None) -> List[int]:
    """Users holding ``holiday:publish`` in scope (01 §8.2); without any, ``holiday:manage`` holders (§8.4)."""
    from app.payroll.services.holders import holder_user_ids

    entities = [legal_entity_id] if legal_entity_id else _publisher_entities(db, company_id)
    for code in ("holiday:publish", "holiday:manage"):
        users = sorted({u for e in entities for u in holder_user_ids(db, company_id, code, e)})
        if users:
            return users
    return []


def _notify_once(db: Session, company_id: int, user_ids: Iterable[int], title: str, message: str, link: str) -> int:
    """In-app notification unless the user already has one with this title and link (idempotent reruns)."""
    from app.models.notification import Notification
    from app.services.notification_service import NotificationService

    sent = 0
    for user_id in sorted(set(user_ids)):
        exists = db.query(Notification.id).filter(Notification.company_id == company_id,
                                                  Notification.user_id == user_id, Notification.title == title,
                                                  Notification.action_url == link).first()
        if exists is None:
            NotificationService.create(db, user_id, title, message, "info", link)
            sent += 1
    return sent


def _notify_draft_ready(db: Session, company_id: int, year: int, totals: Mapping[str, int]) -> int:
    """§8.4: one "drafts are ready" message per publisher and year."""
    message = (f"{year} holiday drafts are ready for {totals['calendars']} calendar(s): {totals['holidays']} "
               f"holiday(s), {totals['needs_date']} need a date, {totals['weekend']} fall on a weekend. "
               f"Review before 1 Jan {year}.")
    return _notify_once(db, company_id, publisher_ids(db, company_id), f"{year} holiday drafts are ready", message,
                        NEXT_YEAR_LINK.format(year=year))


def tenant_today(db: Session, company_id: int) -> date:
    return clock.company_today(db, company_id)


def due_year(db: Session, company_id: int, today: Optional[date] = None) -> Optional[int]:
    """The year the daily check generates: local year + 1 once local today >= generate_on (LH-REC-29)."""
    today = today or tenant_today(db, company_id)
    return today.year + 1 if today >= generate_on(db, company_id, today.year) else None


def schedule_tenant(db: Session, company_id: int, today: Optional[date] = None) -> dict:
    """The DAILY check (§8.1): enqueue the tenant run keyed ``holiday.rollover:{Y+1}`` once its date has come; the key
    makes a missed day, a late tenant or a moved ``generate_on`` self-heal without running twice."""
    from app.payroll.services import jobs

    today = today or tenant_today(db, company_id)
    year = due_year(db, company_id, today)
    if year is None:
        return {"due": False, "generate_on": generate_on(db, company_id, today.year).isoformat()}
    job = jobs.enqueue(db, company_id=company_id, type=ROLLOVER_JOB, payload={"year": year},
                       idempotency_key=f"{ROLLOVER_JOB}:{year}")
    return {"due": True, "year": year, "job_id": getattr(job, "id", job)}


def _jobs():
    from app.payroll.services import jobs

    return jobs


@_jobs().register(ROLLOVER_JOB)
def _rollover_job(ctx) -> dict:
    payload = ctx.payload or {}
    if payload.get("year"):
        return run_rollover(ctx.db, ctx.company_id, int(payload["year"]))
    return schedule_tenant(ctx.db, ctx.company_id)


# ============================================================================================== reminders (§6.3, §8.6)
def provisional_reminders(db: Session, company_id: int, local_date: date) -> int:
    """§6.3: a reminder per PROVISIONAL occurrence (day 1 of a multi-day holiday) whose first day is exactly N days
    after ``local_date`` for N in ``holiday.provisional.confirm_reminder_days``; never twice for (holiday, N)."""
    from datetime import timedelta

    raw = _setting(db, company_id, "holiday.provisional.confirm_reminder_days", [14, 2]) or []
    offsets = sorted({int(n) for n in raw if isinstance(n, (int, float)) and 0 <= int(n) <= 60})
    if not offsets:
        return 0
    targets = {local_date + timedelta(days=n): n for n in offsets}
    rows = db.query(Holiday).filter(Holiday.company_id == company_id, Holiday.status == "PROVISIONAL",
                                    Holiday.date.in_(sorted(targets))).all()
    calendars = {c.id: c for c in db.query(HolidayCalendar).filter(
        HolidayCalendar.company_id == company_id, HolidayCalendar.id.in_({h.calendar_id for h in rows} or {0}))}
    sent = 0
    for h in sorted(rows, key=lambda r: (r.date, r.id)):
        if (h.sequence_index or 1) > 1 or h.calendar_id not in calendars:
            continue
        calendar = calendars[h.calendar_id]
        n = targets[h.date]
        link = f"/holidays/calendars/{calendar.id}?year={h.date.year}&modal=holiday&id={h.id}&mode=confirm"
        message = (f"Confirm {h.name} on {calendar.code}: expected {h.date.strftime('%a %d %b %Y')}. "
                   f"Confirm or change the date.")
        sent += _notify_once(db, company_id, publisher_ids(db, company_id, calendar.legal_entity_id),
                             f"Confirm {h.name} ({n} days)", message, link)
    return sent


def unpublished_reminders(db: Session, company_id: int, local_date: date) -> int:
    """§8.6: on 12-01 and 12-15 (next year) and 01-01 (this year) publishers hear about every calendar whose year is
    not published although it has rules or had holidays the year before."""
    from app.services import holiday_years

    if (local_date.month, local_date.day) not in UNPUBLISHED_REMINDERS:
        return 0
    year = local_date.year + 1 if local_date.month == 12 else local_date.year
    sent = 0
    for calendar in db.query(HolidayCalendar).filter(HolidayCalendar.company_id == company_id,
                                                     HolidayCalendar.status == "ACTIVE").order_by(HolidayCalendar.id):
        if holiday_years.state_of(db, company_id, calendar.id, year) == "PUBLISHED":
            continue
        had = bool(_own_rows(db, company_id, calendar.id, year - 1, year - 1))
        if not had and not active_rules_in(rules_of(db, company_id, calendar.id), year):
            continue
        message = f"{calendar.code} {year} is not published. Employees will have no holidays for {year} until it is."
        sent += _notify_once(db, company_id, publisher_ids(db, company_id, calendar.legal_entity_id),
                             f"{calendar.code} {year} is not published ({local_date.isoformat()})", message,
                             NEXT_YEAR_LINK.format(year=year) + f"&calendar_id={calendar.id}")
    return sent


@_jobs().register(REMINDERS_JOB)
def _reminders_job(ctx) -> dict:
    payload = ctx.payload or {}
    local_date = date.fromisoformat(payload["local_date"]) if payload.get("local_date") else \
        tenant_today(ctx.db, ctx.company_id)
    return {"provisional": provisional_reminders(ctx.db, ctx.company_id, local_date),
            "unpublished": unpublished_reminders(ctx.db, ctx.company_id, local_date)}


# ============================================================================================== Next year page (§8.5)
def overview(db: Session, principal: Principal, year: Optional[int] = None) -> dict:
    """GET /holiday-calendars/rollover?year= (SPINE V3-096): every calendar in scope with the year's state and counts,
    plus the header facts (generate_on, when drafts were generated, n of m published)."""
    from sqlalchemy import or_

    from app.services import holiday_calendar_service as svc, holiday_years

    today = tenant_today(db, principal.company_id)
    year = year or today.year + 1
    query = db.query(HolidayCalendar).filter(HolidayCalendar.company_id == principal.company_id,
                                             HolidayCalendar.status == "ACTIVE")
    entities = svc._visible_entities(db, principal)  # noqa: SLF001
    if entities is not None:
        query = query.filter(or_(HolidayCalendar.legal_entity_id.is_(None),
                                 HolidayCalendar.legal_entity_id.in_(sorted(entities) or [0])))
    items = []
    for calendar in topological(db, principal.company_id, query.all()):
        record = year_record(db, principal.company_id, calendar.id, year)
        state = holiday_years.state_of(db, principal.company_id, calendar.id, year)
        rules = active_rules_in(rules_of(db, principal.company_id, calendar.id), year)
        if record is not None and record.generation_status == "FAILED" and state != "PUBLISHED":
            status = "FAILED"
        elif state in ("PUBLISHED", "DRAFT"):
            status = state
        else:
            status = "NOT_GENERATED" if rules else "NO_RULES"
        items.append({
            "calendar_id": calendar.id, "code": calendar.code, "name": calendar.name,
            "legal_entity_id": calendar.legal_entity_id, "year_status": status,
            "version": record.version if record is not None else (1 if state == "PUBLISHED" else 0),
            "rules": len(rules), "counts": summarise(db, principal.company_id, calendar, year),
            "last_generated_at": record.generated_at.isoformat() if record is not None and record.generated_at else None,
            "failed": status == "FAILED", "error": record.generation_error if record is not None else None,
            "can_manage": svc.can_act(db, principal, calendar),
            "can_publish": svc.can_act(db, principal, calendar, "holiday:publish"),
        })
    generated = [i["last_generated_at"] for i in items if i["last_generated_at"]]
    starts = generate_on(db, principal.company_id, year - 1)
    return {
        "year": year, "local_today": today.isoformat(), "generate_on": starts.isoformat(),
        "before_generate_on": today < starts, "generated_on": min(generated) if generated else None,
        "published": sum(1 for i in items if i["year_status"] == "PUBLISHED"),
        "total": sum(1 for i in items if i["year_status"] != "NO_RULES"),
        "items": items,
    }


def generate_missing(db: Session, principal: Principal, year: int) -> dict:
    """"Generate drafts now" (§8.5 header): the calendars in the caller's scope that lack a draft (or failed)."""
    from app.core.errors import AppError, conflict
    from app.services import holiday_calendar_service as svc

    today = tenant_today(db, principal.company_id)
    if year < today.year:
        raise conflict(f"{year} has passed; past years are not generated.", "HOLIDAY_IN_PAST")
    if year > today.year + 2:
        raise AppError(422, f"Years can be prepared up to {today.year + 2}.", "FUTURE_LIMIT")
    ids = [c.id for c in db.query(HolidayCalendar).filter(HolidayCalendar.company_id == principal.company_id,
                                                          HolidayCalendar.status == "ACTIVE")
           if svc.can_act(db, principal, c)]
    return run_rollover(db, principal.company_id, year, principal, calendar_ids=ids, only_missing=True,
                        notify=False)
