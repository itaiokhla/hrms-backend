"""Holiday imports: CSV and ICS into one calendar year (LH 04-holiday/02 §7.4, §9.1-§9.3; 11 §4.2.14; WI-1.8).

One flow for every kind ("never a silent overwrite", REQ-HT-11): ``dry_run`` parses and validates the file, matches
each line against the target year's own rows and stores the report (``holiday_imports``, status DRY_RUN) — nothing
else is written; ``commit`` applies the accepted lines, against the same year content (``target_hash``, else 409
``ROW_VERSION_CONFLICT``): straight into a DRAFT (or new) year, as one amendment into a PUBLISHED year (needs
``holiday:publish`` and a reason). Rows that touch a past date are line errors (``HOLIDAY_IN_PAST``); backdated
closures are never imported.

Kinds: ``CSV_DATED`` ("Holidays (dates)" template), ``CSV_RULES`` ("Repeating holidays"), ``ICS`` (VEVENTs: dated
rows, RRULEs of the accepted subset become rules). Cells are sanitised (control characters and markup removed,
trimmed, length-checked); a cell starting with ``=``, ``+``, ``-`` or ``@`` stays plain text (never evaluated) and is
escaped again on export (``escape_csv_cell``). Limits 2 MB / 5,000 rows (``IMPORT_TOO_LARGE``).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.principal import Principal
from app.models.holiday import Holiday, HolidayImport
from app.services import holiday_recurrence as rec
from app.services.audit_log_service import AuditLogService

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
DRY_RUN_DAYS = 7
KINDS = ("CSV_DATED", "CSV_RULES", "ICS")
TYPES = ("PUBLIC", "COMPANY", "OPTIONAL")
STATUSES = ("CONFIRMED", "PROVISIONAL")
CLASSES = ("NEW", "CHANGED_DATE", "CHANGED_DETAILS", "UNCHANGED", "CONFLICT", "CONFLICT_EDIT", "NOT_IN_SOURCE",
           "CANCEL", "NEW_RULE", "DUPLICATE_RULE", "ERROR")
DEFAULT_ACCEPT = {"NEW", "CHANGED_DATE", "CHANGED_DETAILS", "CANCEL", "NEW_RULE"}
DATED_COLUMNS = ("date", "name", "days", "type", "category", "portion", "status", "description", "legal_ref",
                 "issued_on")
RULE_COLUMNS = ("name", "kind", "month", "day", "weekday", "nth", "direction", "offset_days", "tradition",
                "missing_date", "expected_month", "days", "days_max", "observed_rule", "type", "category", "portion",
                "from_year", "until_year")
TEMPLATES = {
    "CSV_DATED": ("date,name,days,type,category,portion,status,description,legal_ref,issued_on\n"
                  "{y}-01-26,Republic Day,1,PUBLIC,NATIONAL,FULL,CONFIRMED,,,\n"
                  "{y}-03-20,Eid al-Fitr,2,PUBLIC,FESTIVAL,FULL,PROVISIONAL,Expected date,,\n"),
    "CSV_RULES": ("name,kind,month,day,weekday,nth,direction,offset_days,tradition,missing_date,expected_month,days,"
                  "days_max,observed_rule,type,category,portion,from_year,until_year\n"
                  "Republic Day,FIXED_DATE,1,26,,,,,,,,1,,NONE,PUBLIC,NATIONAL,FULL,{y},\n"
                  "Thanksgiving,NTH_WEEKDAY,11,,THU,4,,,,,,1,,NONE,PUBLIC,NATIONAL,FULL,{y},\n"
                  "Good Friday,EASTER_OFFSET,,,,,,-2,WESTERN,,,1,,NONE,PUBLIC,FESTIVAL,FULL,{y},\n"),
}
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
            "MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_TAGS = re.compile(r"<[^>]*>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IGNORED_ICS = ("TRANSP", "CLASS", "VALARM", "ORGANIZER", "ATTENDEE")


# ============================================================================================== sanitising
def clean_text(value: Any, limit: int) -> Tuple[Optional[str], Optional[str]]:
    """(text, error): control characters and markup removed, whitespace collapsed, at most ``limit`` characters.
    A leading ``=``/``+``/``-``/``@`` is kept as text — the value is never evaluated (formula injection)."""
    if value is None:
        return None, None
    text = unicodedata.normalize("NFC", str(value))
    text = _TAGS.sub("", _CONTROL.sub("", text))
    text = " ".join(text.split())
    if not text:
        return None, None
    if len(text) > limit:
        return text[:limit], f"At most {limit} characters."
    return text, None


def normalise_name(name: str) -> str:
    """§8.6 normalisation: casefold, punctuation removed, inner spaces collapsed."""
    text = re.sub(r"[^\w\s]", " ", (name or "").casefold())
    return " ".join(text.split())


# ============================================================================================== parsing
def _rows_of_csv(data: bytes) -> Tuple[List[str], List[Tuple[int, Dict[str, str]]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError(422, "The file must be UTF-8 text.", "IMPORT_INVALID_FILE")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise AppError(422, "The file is empty.", "IMPORT_INVALID_FILE")
    columns = [c.strip().lower() for c in header]
    rows = []
    for number, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        rows.append((number, {columns[i]: cells[i] for i in range(min(len(columns), len(cells))) if columns[i]}))
        if len(rows) > MAX_IMPORT_ROWS:
            raise AppError(422, f"An import holds at most {MAX_IMPORT_ROWS} rows.", "IMPORT_TOO_LARGE")
    return columns, rows


def _enum(value: Optional[str], allowed: Sequence[str], default: str, field: str, errors: List[dict]) -> str:
    if value is None or not str(value).strip():
        return default
    text = str(value).strip().upper().replace(" ", "_")
    if text not in allowed:
        errors.append({"field": field, "message": f"Use one of {', '.join(allowed)}."})
        return default
    return text


def _date(value: Optional[str], field: str, errors: List[dict], *, required: bool = True) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        if required:
            errors.append({"field": field, "message": "Required (YYYY-MM-DD)."})
        return None
    if not _DATE.match(text):
        errors.append({"field": field, "message": f"{text!r} is not a YYYY-MM-DD date."})
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        errors.append({"field": field, "message": f"{text} is not a calendar date."})
        return None


def _int(value: Optional[str], field: str, errors: List[dict], low: int, high: int, default: Optional[int] = None):
    text = (value or "").strip()
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        errors.append({"field": field, "message": f"{text!r} is not a whole number."})
        return default
    if not low <= number <= high:
        errors.append({"field": field, "message": f"Use {low}-{high}."})
        return default
    return number


def _dated_line(number: int, row: Mapping[str, str], target_year: int) -> dict:
    errors: List[dict] = []
    warnings: List[dict] = []
    name, problem = clean_text(row.get("name"), 150)
    if problem:
        errors.append({"field": "name", "message": problem})
    if not name:
        errors.append({"field": "name", "message": "Required."})
    day = _date(row.get("date"), "date", errors)
    if day is not None and day.year != target_year:
        errors.append({"field": "date", "message": f"Date must be in {target_year}."})
    kind = _enum(row.get("type"), TYPES, "COMPANY", "type", errors)
    portion = _enum(row.get("portion"), rec.PORTIONS, "FULL", "portion", errors)
    if portion != "FULL":
        errors.append({"field": "portion", "code": "INVALID_PORTION",
                       "message": "Half-day holidays arrive with LH-M5b; use FULL."})
    description, problem = clean_text(row.get("description"), 500)
    if problem:
        errors.append({"field": "description", "message": problem})
    legal_ref, problem = clean_text(row.get("legal_ref"), 100)
    if problem:
        errors.append({"field": "legal_ref", "message": problem})
    i18n = {}
    for key, value in row.items():
        if key.startswith("name_") and value and value.strip():
            locale = key[5:]
            text, _ = clean_text(value, 150)
            if not re.match(r"^[a-z]{2}(-[A-Z]{2})?$", locale):
                warnings.append({"field": key, "message": f"Unknown locale {locale!r} ignored."})
            elif text:
                i18n[locale] = text
    issued = _date(row.get("issued_on"), "issued_on", errors, required=False)
    values = {"date": day.isoformat() if day else None, "name": name,
              "days": _int(row.get("days"), "days", errors, 1, 31, 1), "holiday_type": kind,
              "category": _enum(row.get("category"), rec.HOLIDAY_CATEGORIES, "OTHER", "category", errors),
              "portion": portion, "status": _enum(row.get("status"), STATUSES, "CONFIRMED", "status", errors),
              "description": description, "legal_ref": legal_ref,
              "issued_on": issued.isoformat() if issued else None, "name_i18n": i18n}
    return {"line": number, "kind": "DATED", "values": values, "errors": errors, "warnings": warnings}


def _weekday(value: Optional[str], errors: List[dict]) -> Optional[int]:
    text = (value or "").strip().upper()
    if not text:
        return None
    if text.isdigit() and 0 <= int(text) <= 6:
        return int(text)
    if text[:3] in WEEKDAYS:
        return WEEKDAYS[text[:3]]
    errors.append({"field": "weekday", "message": "Use MON..SUN or 0-6."})
    return None


def _rule_line(number: int, row: Mapping[str, str], target_year: int) -> dict:
    errors: List[dict] = []
    name, problem = clean_text(row.get("name"), 150)
    if problem:
        errors.append({"field": "name", "message": problem})
    if not name:
        errors.append({"field": "name", "message": "Required."})
    kind = _enum(row.get("kind"), tuple(k for k in rec.KINDS if k != "RRULE"), "FIXED_DATE", "kind", errors)
    params: Dict[str, Any] = {}
    month = _int(row.get("month"), "month", errors, 1, 12)
    day = _int(row.get("day"), "day", errors, 1, 31)
    weekday = _weekday(row.get("weekday"), errors)
    nth_text = (row.get("nth") or "").strip().upper()
    nth = -1 if nth_text == "LAST" else _int(row.get("nth"), "nth", errors, 1, 5)
    if kind == "FIXED_DATE":
        params = {"month": month, "day": day}
    elif kind == "NTH_WEEKDAY":
        params = {"month": month, "weekday": weekday, "nth": nth}
    elif kind == "WEEKDAY_RELATIVE":
        params = {"month": month, "day": day, "weekday": weekday,
                  "direction": _enum(row.get("direction"), rec.DIRECTIONS, "ON_OR_AFTER", "direction", errors)}
    elif kind == "EASTER_OFFSET":
        params = {"offset_days": _int(row.get("offset_days"), "offset_days", errors, -70, 70, 0),
                  "tradition": _enum(row.get("tradition"), rec.TRADITIONS, "WESTERN", "tradition", errors)}
    elif kind == "ANNUAL_CONFIRM":
        expected = _int(row.get("expected_month"), "expected_month", errors, 1, 12)
        params = {"expected_month": expected} if expected else {}
    elif kind == "ONE_OFF":
        one = _date(row.get("day") if _DATE.match((row.get("day") or "").strip()) else None, "day", errors)
        params = {"date": one.isoformat() if one else None}
    if kind == "FIXED_DATE" and (row.get("missing_date") or "").strip():
        params["missing_date"] = _enum(row.get("missing_date"), rec.MISSING_DATES, "SKIP", "missing_date", errors)
    if kind in ("FIXED_DATE", "NTH_WEEKDAY") and (row.get("offset_days") or "").strip():
        params["offset_days"] = _int(row.get("offset_days"), "offset_days", errors, -31, 31, 0)
    values = {"name": name, "kind": kind, "params": params,
              "holiday_type": _enum(row.get("type"), TYPES, "COMPANY", "type", errors),
              "category": _enum(row.get("category"), rec.HOLIDAY_CATEGORIES, "OTHER", "category", errors),
              "portion": _enum(row.get("portion"), rec.PORTIONS, "FULL", "portion", errors),
              "observed_rule": _enum(row.get("observed_rule"), rec.OBSERVED_RULES, "NONE", "observed_rule", errors),
              "duration_days": _int(row.get("days"), "days", errors, 1, 31, 1),
              "valid_from_year": _int(row.get("from_year"), "from_year", errors, 2000, 2100, target_year),
              "valid_to_year": _int(row.get("until_year"), "until_year", errors, 2000, 2100)}
    if kind == "ANNUAL_CONFIRM" and (row.get("days_max") or "").strip():
        values["duration_days_max"] = _int(row.get("days_max"), "days_max", errors, 1, 31)
    return {"line": number, "kind": "RULE", "values": values, "errors": errors, "warnings": []}


def unfold(text: str) -> List[str]:
    """RFC 5545 §3.1: a line starting with a space or tab continues the previous one."""
    lines: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";")
            .replace("\\\\", "\\"))


def _ics_property(line: str) -> Tuple[str, Dict[str, str], str]:
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        key, _, val = p.partition("=")
        params[key.upper()] = val
    return parts[0].upper(), params, value


def _ics_date(value: str, params: Mapping[str, str]) -> Tuple[Optional[date], Optional[str]]:
    text = value.strip()
    if params.get("VALUE", "").upper() == "DATE-TIME" or "T" in text:
        return None, ("Timed events are not holidays. Import them as half-day holidays (from LH-M5b) or leave "
                      "them out.")
    if not re.match(r"^\d{8}$", text):
        return None, f"{text!r} is not a DATE value (YYYYMMDD)."
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8])), None
    except ValueError:
        return None, f"{text} is not a calendar date."


def _ics_events(data: bytes) -> Tuple[Optional[str], List[Tuple[int, Dict[str, Tuple[Dict[str, str], str]]]], set]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AppError(422, "The calendar file must be UTF-8 text.", "IMPORT_INVALID_FILE")
    lines = unfold(text)
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise AppError(422, "Not an iCalendar file (BEGIN:VCALENDAR missing).", "IMPORT_INVALID_FILE")
    calname, events, ignored = None, [], set()
    current: Optional[Dict[str, Tuple[Dict[str, str], str]]] = None
    nested = 0
    for line in lines:
        name, params, value = _ics_property(line)
        if name == "X-WR-CALNAME":
            calname = clean_text(_unescape(value), 100)[0]
        if name == "BEGIN" and value.upper() == "VEVENT":
            current = {}
            nested = 0
            events.append((len(events) + 1, current))
            if len(events) > MAX_IMPORT_ROWS:
                raise AppError(422, f"An import holds at most {MAX_IMPORT_ROWS} events.", "IMPORT_TOO_LARGE")
            continue
        if current is None:
            continue
        if name == "BEGIN":
            nested += 1
            ignored.add(value.upper())
            continue
        if name == "END":
            if value.upper() == "VEVENT":
                current = None
            else:
                nested = max(nested - 1, 0)
            continue
        if nested:
            continue
        if name in _IGNORED_ICS or (name.startswith("X-") and name != "X-WR-CALNAME"):
            ignored.add(name)
            continue
        if name == "SUMMARY" and params.get("LANGUAGE"):
            current.setdefault("_I18N", ({}, ""))[0][params["LANGUAGE"].lower()] = _unescape(value)
            if "SUMMARY" in current:
                continue
        current.setdefault(name, (params, value))
    return calname, events, ignored


def _ics_line(number: int, event: Mapping[str, Tuple[Dict[str, str], str]], target_year: int) -> dict:
    errors: List[dict] = []
    warnings: List[dict] = []
    name, problem = clean_text(_unescape(event.get("SUMMARY", ({}, ""))[1]), 150)
    if problem:
        errors.append({"field": "SUMMARY", "message": problem})
    if not name:
        errors.append({"field": "SUMMARY", "message": "Required."})
    if "DTSTART" not in event:
        errors.append({"field": "DTSTART", "message": "Required."})
        start = None
    else:
        start, problem = _ics_date(event["DTSTART"][1], event["DTSTART"][0])
        if problem:
            errors.append({"field": "DTSTART", "message": problem})
    days = 1
    if start is not None and "DTEND" in event:
        end, problem = _ics_date(event["DTEND"][1], event["DTEND"][0])
        if problem:
            errors.append({"field": "DTEND", "message": problem})
        elif end is not None:
            days = (end - start).days  # DTEND is exclusive (RFC 5545 §3.6.1)
    elif "DURATION" in event:
        match = re.match(r"^P(\d+)D$", event["DURATION"][1].strip().upper())
        if match:
            days = int(match.group(1))
        else:
            errors.append({"field": "DURATION", "message": "Only whole days (P1D, P3D)."})
    if not 1 <= days <= 31:
        errors.append({"field": "DTEND", "message": "A holiday lasts 1-31 days."})
        days = 1
    status_text = event.get("STATUS", ({}, "CONFIRMED"))[1].strip().upper()
    status = {"TENTATIVE": "PROVISIONAL", "CANCELLED": "CANCELLED"}.get(status_text, "CONFIRMED")
    category = "OTHER"
    for cat in event.get("CATEGORIES", ({}, ""))[1].split(","):
        if cat.strip().upper().replace(" ", "_") in rec.HOLIDAY_CATEGORIES:
            category = cat.strip().upper().replace(" ", "_")
            break
    description = clean_text(_unescape(event.get("DESCRIPTION", ({}, ""))[1]), 500)[0]
    uid = clean_text(event.get("UID", ({}, ""))[1], 200)[0]
    i18n = {k: clean_text(v, 150)[0] for k, v in event.get("_I18N", ({}, ""))[0].items()}
    if "RRULE" in event and start is not None and not errors:
        try:
            parts = rec.rrule_parse(event["RRULE"][1])
            native = rec.rrule_to_native(parts, start)
        except rec.RecurrenceError as exc:
            return {"line": number, "kind": "RULE", "values": {"name": name, "rrule": event["RRULE"][1]},
                    "errors": [{"field": "RRULE", "code": "RULE_INVALID", "reason": "RRULE_UNSUPPORTED",
                                "message": getattr(exc, "message", None) or str(exc)}],
                    "warnings": warnings, "uid": uid}
        if native is None:
            errors.append({"field": "RRULE", "message": "The rule has no date."})
        else:
            values = {"name": name, "kind": native.kind, "params": dict(native.params), "holiday_type": "COMPANY",
                      "category": category, "portion": "FULL", "observed_rule": "NONE", "duration_days": days,
                      "valid_from_year": max(native.valid_from_year, target_year),
                      "valid_to_year": native.valid_to_year, "description": description}
            return {"line": number, "kind": "RULE", "values": values, "errors": [], "warnings": warnings, "uid": uid}
    if start is not None and start.year != target_year:
        errors.append({"field": "DTSTART", "message": f"Date must be in {target_year}."})
    values = {"date": start.isoformat() if start else None, "name": name, "days": days, "holiday_type": "COMPANY",
              "category": category, "portion": "FULL", "status": status, "description": description,
              "legal_ref": None, "issued_on": None, "name_i18n": {k: v for k, v in i18n.items() if v},
              "external_uid": uid}
    return {"line": number, "kind": "DATED", "values": values, "errors": errors, "warnings": warnings, "uid": uid}


def parse(kind: str, data: bytes, target_year: int) -> Tuple[List[dict], dict]:
    """(lines, extra) of a file; file-level problems raise (IMPORT_TOO_LARGE, IMPORT_INVALID_FILE)."""
    if len(data) > MAX_IMPORT_BYTES:
        raise AppError(422, "An import file is at most 2 MB.", "IMPORT_TOO_LARGE")
    if kind == "ICS":
        calname, events, ignored = _ics_events(data)
        return [_ics_line(n, e, target_year) for n, e in events], {"source_label": calname,
                                                                    "ignored_properties": sorted(ignored)}
    columns, rows = _rows_of_csv(data)
    needed = ("date", "name") if kind == "CSV_DATED" else ("name", "kind")
    missing = [c for c in needed if c not in columns]
    if missing:
        raise AppError(422, f"The header lacks {', '.join(missing)}. Download the template.", "IMPORT_INVALID_FILE",
                       extra={"details": {"missing_columns": missing}})
    known = set(DATED_COLUMNS if kind == "CSV_DATED" else RULE_COLUMNS)
    unknown = [c for c in columns if c and c not in known and not (kind == "CSV_DATED" and c.startswith("name_"))]
    build = _dated_line if kind == "CSV_DATED" else _rule_line
    return [build(n, r, target_year) for n, r in rows], {"ignored_columns": unknown}


# ============================================================================================== matching (§9.3)
def _row_json(h: Holiday) -> dict:
    return {"id": h.id, "date": h.date.isoformat(), "name": h.name, "holiday_type": h.holiday_type,
            "category": h.category, "status": h.status, "source": h.source}


def classify(lines: List[dict], rows: Sequence[Holiday], today: date, *, source_id: Optional[str] = None) -> None:
    """Classify every DATED line against the year's own rows (first match wins, §9.3) in place."""
    live = [h for h in rows if h.status != "CANCELLED"]
    by_uid = {(h.source_ref or {}).get("external_uid"): h for h in live if (h.source_ref or {}).get("external_uid")}
    used: set = set()
    for line in lines:
        values = line["values"]
        if line["errors"]:
            line["classification"] = "ERROR"
            continue
        if line["kind"] != "DATED":
            continue
        day = date.fromisoformat(values["date"])
        if day <= today:
            line["errors"].append({"field": "date", "code": "HOLIDAY_IN_PAST",
                                   "message": "Past dates are kept for payroll history; record a closure instead."})
            line["classification"] = "ERROR"
            continue
        name = normalise_name(values["name"])
        match = by_uid.get(values.get("external_uid")) if values.get("external_uid") else None
        how = "uid" if match is not None else None
        if match is None:
            match = next((h for h in live if h.date == day and normalise_name(h.name) == name), None)
            how = "date_name" if match is not None else None
        if match is None:
            match = next((h for h in live if normalise_name(h.name) == name and abs((h.date - day).days) <= 7), None)
            how = "moved" if match is not None else None
        if match is None:
            dup = next((h for h in live if h.date == day and h.category == values["category"]), None)
            if dup is not None:
                line.update(classification="CONFLICT", match=_row_json(dup),
                            note=f"Possible duplicate of {dup.name} on the same date")
                continue
            if values["status"] == "CANCELLED":
                line.update(classification="UNCHANGED", note="Cancelled in the source; nothing to cancel here")
            else:
                line["classification"] = "NEW"
            continue
        used.add(match.id)
        line["match"] = _row_json(match)
        ref = match.source_ref or {}
        if match.source == "MANUAL" and how != "uid" or ref.get("overridden"):
            same = match.date == day and match.name == values["name"]
            line["classification"] = "UNCHANGED" if same else "CONFLICT_EDIT"
            if not same:
                line["note"] = "You edited this holiday; your version is kept unless you accept the file's"
            continue
        if values["status"] == "CANCELLED":
            line["classification"] = "CANCEL"
        elif match.date != day:
            line["classification"] = "CHANGED_DATE"
        elif (match.name, match.holiday_type, match.category, match.status) != (
                values["name"], values["holiday_type"], values["category"], values["status"]):
            line["classification"] = "CHANGED_DETAILS"
        else:
            line["classification"] = "UNCHANGED"
    if source_id:
        for h in live:
            if h.id not in used and (h.source_ref or {}).get("source_id") == source_id and h.date > today:
                lines.append({"line": None, "kind": "DATED", "values": {"date": h.date.isoformat(), "name": h.name},
                              "errors": [], "warnings": [], "classification": "NOT_IN_SOURCE",
                              "match": _row_json(h)})


def _classify_rules(db: Session, principal: Principal, calendar_id: int, lines: List[dict], target_year: int) -> None:
    """Rule lines: preview for the target year through the rule editor's own validation (RV-01..09)."""
    from app.services import holiday_years

    for line in lines:
        if line["kind"] != "RULE" or line.get("classification") == "ERROR" or line["errors"]:
            if line["kind"] == "RULE":
                line["classification"] = "ERROR"
            continue
        values = {k: v for k, v in line["values"].items() if v is not None and k != "rrule"}
        try:
            preview = holiday_years.preview_rule(db, principal, {"calendar_id": calendar_id, "rule": values,
                                                                 "years": 1, "from_year": target_year})
        except AppError as exc:
            details = exc.extra.get("details") if getattr(exc, "extra", None) else None
            line["errors"].append({"field": "rule", "code": exc.code, "message": str(exc.detail),
                                   "details": details})
            line["classification"] = "DUPLICATE_RULE" if exc.code == "RULE_DUPLICATE" else "ERROR"
            continue
        rows = preview["years"][0]["rows"] if preview["years"] else []
        line["preview"] = [{"date": r["date"], "name": r["name"]} for r in rows]
        dup = [i for i in preview.get("info", []) if i.get("code") in ("RULE_DUPLICATE", "RV-08")]
        line["classification"] = "DUPLICATE_RULE" if dup else "NEW_RULE"
        if dup:
            line["note"] = dup[0]["message"]


# ============================================================================================== dry run / commit
def _calendar(db: Session, principal: Principal, calendar_id: int):
    from app.services import holiday_calendar_service as svc

    calendar = svc._get_managed(db, principal, calendar_id)  # noqa: SLF001
    if calendar.status != "ACTIVE":
        raise bad_request(f"Holiday calendar {calendar.code} is inactive.", "ORG_DIMENSION_INACTIVE")
    return calendar


def _summary(lines: Sequence[dict]) -> dict:
    out = {c: 0 for c in CLASSES}
    for line in lines:
        cls = line.get("classification", "ERROR")
        out[cls] = out.get(cls, 0) + 1
    out["lines"] = len(lines)
    return out


def import_json(row: HolidayImport, *, lines: bool = True) -> dict:
    report = row.report or {}
    out = {"id": row.id, "calendar_id": row.calendar_id, "kind": row.kind, "target_year": row.target_year,
           "status": row.status, "file_name": row.file_name, "sha256": row.sha256, "target_hash": row.target_hash,
           "row_version": row.row_version, "created_at": row.created_at.isoformat() if row.created_at else None,
           "committed_at": row.committed_at.isoformat() if row.committed_at else None,
           "summary": report.get("summary", {}), "year_status": report.get("year_status"),
           "source_label": report.get("source_label"), "ignored_properties": report.get("ignored_properties", []),
           "ignored_columns": report.get("ignored_columns", []), "result": report.get("result")}
    if lines:
        out["lines"] = report.get("lines", [])
    return out


def dry_run(db: Session, principal: Principal, calendar_id: int, *, kind: str, target_year: int, data: bytes,
            file_name: Optional[str] = None) -> dict:
    """POST /holiday-calendars/{id}/imports: parse, validate, match, store the report (status DRY_RUN)."""
    from app.core import storage
    from app.services import holiday_calendar_service as svc, holiday_years

    if kind not in KINDS:
        raise AppError(422, f"kind must be one of {', '.join(KINDS)}.", "VALIDATION_ERROR")
    calendar = _calendar(db, principal, calendar_id)
    today = svc.calendar_local_today(db, principal.company_id, calendar.id)
    if target_year < today.year or target_year > today.year + holiday_years.FUTURE_YEARS:
        raise AppError(422, f"Import into {today.year}-{today.year + holiday_years.FUTURE_YEARS}.", "FUTURE_LIMIT")
    lines, extra = parse(kind, data, target_year)
    rows = holiday_years.year_rows(db, principal.company_id, calendar.id, target_year)
    source_id = f"ics:{extra['source_label']}" if kind == "ICS" and extra.get("source_label") else None
    classify(lines, rows, today, source_id=source_id)
    _classify_rules(db, principal, calendar.id, lines, target_year)
    for line in lines:
        line["action"] = "ACCEPT" if line.get("classification") in DEFAULT_ACCEPT else "SKIP"
    digest = hashlib.sha256(data).hexdigest()
    path = storage.save_bytes(principal.company_id, "holiday-imports", data, "ics" if kind == "ICS" else "csv")
    state = holiday_years.state_of(db, principal.company_id, calendar.id, target_year)
    report = {"lines": lines, "summary": _summary(lines), "year_status": state, "source_id": source_id, **extra}
    row = HolidayImport(company_id=principal.company_id, calendar_id=calendar.id, kind=kind, target_year=target_year,
                        status="DRY_RUN", file_name=clean_text(file_name, 200)[0], file_path=path, sha256=digest,
                        source_id=source_id, report=report,
                        target_hash=holiday_years.content_hash(db, principal.company_id, calendar.id, target_year),
                        created_by_user_id=principal.user_id)
    db.add(row)
    db.flush()
    AuditLogService.log(db, principal, "Holidays", "IMPORT_DRY_RUN", f"{calendar.code} {target_year}: import checked",
                        "holiday_import", row.id, details={"kind": kind, "summary": report["summary"]})
    return import_json(row)


def get_import(db: Session, principal: Principal, import_id: int) -> HolidayImport:
    from app.services import holiday_calendar_service as svc

    row = db.query(HolidayImport).filter(HolidayImport.company_id == principal.company_id,
                                         HolidayImport.id == int(import_id)).first()
    if row is None:
        raise not_found("Import")
    svc._get_visible(db, principal, row.calendar_id)  # noqa: SLF001
    if row.status == "DRY_RUN" and row.created_at is not None and \
            row.created_at < clock.utcnow() - timedelta(days=DRY_RUN_DAYS):
        expire(db, row)
    return row


def expire(db: Session, row: HolidayImport) -> None:
    from app.core import storage

    row.status = "EXPIRED"
    storage.delete(row.company_id, row.file_path)
    row.file_path = None
    db.flush()


def _dated_op(values: Mapping[str, Any]) -> dict:
    return {"op": "add", "name": values["name"], "date": values["date"], "holiday_type": values["holiday_type"],
            "category": values["category"], "portion": values["portion"], "status": values["status"],
            "duration_days": values.get("days") or 1, "description": values.get("description"),
            "legal_ref": values.get("legal_ref"), "issued_on": values.get("issued_on")}


def _tag_rows(db: Session, row: HolidayImport, ids: Iterable[int],
              uids: Mapping[Tuple[str, str], Optional[str]]) -> None:
    """Created rows become ``source = IMPORT`` with the import id, the source id and (ICS) the event UID, matched by
    the first day's (date, normalised name)."""
    from app.services.holiday_service import _amending

    _amending(db)
    for h in db.query(Holiday).filter(Holiday.company_id == row.company_id, Holiday.id.in_(list(ids) or [0])):
        ref = dict(h.source_ref or {})
        ref.update({"import_id": row.id, "source_id": row.source_id})
        uid = uids.get((h.date.isoformat(), normalise_name(h.name)))
        if uid:
            ref["external_uid"] = uid
        h.source, h.source_ref = "IMPORT", ref
    db.flush()


def commit(db: Session, principal: Principal, import_id: int, *, decisions: Sequence[Mapping[str, Any]] = (),
           reason: Optional[str] = None, target_hash: Optional[str] = None,
           calendar_id: Optional[int] = None) -> Tuple[int, dict]:
    """POST …/imports/{import_id}/commit: apply the accepted lines in one transaction (§9.1 steps 4-5)."""
    from app.services import holiday_years

    row = get_import(db, principal, import_id)
    if calendar_id is not None and row.calendar_id != int(calendar_id):
        raise not_found("Import")
    if row.status != "DRY_RUN":
        raise conflict(f"This import is {row.status.lower().replace('_', ' ')}; run the check again.",
                       "IMPORT_NOT_OPEN")
    calendar = _calendar(db, principal, row.calendar_id)
    current = holiday_years.content_hash(db, principal.company_id, calendar.id, row.target_year)
    if (target_hash and target_hash != row.target_hash) or current != row.target_hash:
        raise conflict("The calendar changed since the check. Run the check again.", "ROW_VERSION_CONFLICT")
    report = row.report or {}
    lines = report.get("lines", [])
    chosen = {int(d["line"]): str(d.get("action", "")).upper() for d in decisions if d.get("line") is not None}
    accepted = [ln for ln in lines if ln.get("line") is not None
                and chosen.get(ln["line"], ln.get("action")) == "ACCEPT" and not ln.get("errors")]
    state = holiday_years.state_of(db, principal.company_id, calendar.id, row.target_year)
    created_rules, ops, uid_of_op = [], [], []
    for line in accepted:
        cls, values = line.get("classification"), line["values"]
        if cls == "NEW_RULE":
            rule = {k: v for k, v in values.items() if v is not None and k not in ("rrule", "description")}
            created_rules.append(holiday_years.create_rule(db, principal, calendar.id, rule))
        elif cls == "NEW":
            ops.append(_dated_op(values))
            uid_of_op.append(values.get("external_uid"))
        elif cls in ("CHANGED_DATE", "CHANGED_DETAILS", "CONFLICT_EDIT", "CONFLICT") and line.get("match"):
            hid = line["match"]["id"]
            if cls == "CONFLICT":
                ops.append(_dated_op(values))
                uid_of_op.append(values.get("external_uid"))
                continue
            if line["match"]["date"] != values["date"]:
                ops.append({"op": "move", "holiday_id": hid, "to_date": values["date"]})
            ops.append({"op": "edit", "holiday_id": hid, "name": values["name"],
                        "holiday_type": values["holiday_type"], "category": values["category"]})
            uid_of_op.append(None)
        elif cls == "CANCEL" and line.get("match"):
            ops.append({"op": "cancel", "holiday_id": line["match"]["id"]})
            uid_of_op.append(None)
    created: List[int] = []
    status = 200
    body: dict = {}
    if ops and state == "PUBLISHED":
        if not reason or not str(reason).strip():
            raise AppError(422, f"{row.target_year} is published: give a reason for the change.", "REASON_REQUIRED")
        status, body = holiday_years.amend_year(db, principal, calendar.id, row.target_year, ops, reason=reason,
                                                acknowledged_warnings=[w for w in ("HOLIDAY_ON_WEEKEND",
                                                                                   "PROVISIONAL_HOLIDAYS")])
        created = list(body.get("created") or ())
    elif ops:
        for op in ops:
            if op["op"] == "add":
                _, made = holiday_years.add_occurrence(db, principal, calendar.id, op)
                created.append(made["id"])
            elif op["op"] == "cancel":
                holiday_years.edit_draft_row(db, principal, op["holiday_id"], {"action": "SKIP"})
            elif op["op"] == "move":
                holiday_years.edit_draft_row(db, principal, op["holiday_id"], {"date": op["to_date"]})
            elif op["op"] == "edit":
                holiday_years.edit_draft_row(db, principal, op["holiday_id"], {
                    k: op[k] for k in ("name", "holiday_type", "category")})
    if status == 200:
        uids = {(o["date"], normalise_name(o["name"])): u for o, u in zip(ops, uid_of_op) if o["op"] == "add" and u}
        _tag_rows(db, row, created, uids)
        row.status = "COMMITTED"
        row.committed_at = clock.utcnow()
        row.committed_by_user_id = principal.user_id
    result = {"accepted": len(accepted), "rules_created": len(created_rules), "rows_created": len(created),
              "operations": len(ops), "amendment_status": body.get("status")}
    row.report = {**report, "result": result,
                  "lines": [{**ln, "action": chosen.get(ln.get("line"), ln.get("action"))} for ln in lines]}
    row.row_version = (row.row_version or 1) + 1
    db.flush()
    if status == 200:
        AuditLogService.log(db, principal, "Holidays", "IMPORT_COMMITTED",
                            f"{calendar.code} {row.target_year}: import committed", "holiday_import", row.id,
                            details=result)
    return status, {**import_json(row), "amendment": body or None}


def errors_csv(row: HolidayImport) -> str:
    """"errors.csv": one row per line error, cells escaped against formula injection."""
    from app.payroll.banking.files import escape_csv_cell

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["line", "field", "message"])
    for line in (row.report or {}).get("lines", []):
        for error in line.get("errors", []):
            writer.writerow([line.get("line"), escape_csv_cell(str(error.get("field") or "")),
                             escape_csv_cell(str(error.get("message") or ""))])
    return out.getvalue()


def template(kind: str, year: int) -> str:
    if kind not in TEMPLATES:
        raise not_found("Template")
    return TEMPLATES[kind].format(y=year)


def recent(db: Session, principal: Principal, calendar_id: int, limit: int = 20) -> List[dict]:
    from app.services import holiday_calendar_service as svc

    svc._get_visible(db, principal, calendar_id)  # noqa: SLF001
    rows = (db.query(HolidayImport).filter(HolidayImport.company_id == principal.company_id,
                                           HolidayImport.calendar_id == calendar_id)
            .order_by(HolidayImport.id.desc()).limit(limit).all())
    return [import_json(r, lines=False) for r in rows]
