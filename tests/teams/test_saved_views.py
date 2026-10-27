"""Saved views, the LIST_KEYS registry and the bulk-scope rule (X-M3 WI-X-3.4; X 04 §2.2, §2.8, §2.10, §2.13).

X-UI-01 (same count, different set), X-UI-02 (view_id of someone else's private view or another tenant's),
X-UI-03 (search wildcards are literal), X-UI-04 (a shared view's columns are re-validated), AS-UX-01 (one system-view
catalogue equal to X 04 §2.8), limits, sharing, defaults, unknown list keys and BUG-AS-55 (audit list envelope).
"""

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.attendance_helpers import owner_rows
from tests.helpers import create_employee, invite_and_activate, signup_company

X04 = Path(__file__).resolve().parents[3] / "docs" / "cross-module" / "04-ui-patterns-list-detail-forms.md"


@pytest.fixture
def co(app):
    admin = signup_company(app, "Views Co")
    karan = invite_and_activate(app, admin, create_employee(admin, "Karan", "Desai"), ["payroll_manager", "employee"])
    kavya = invite_and_activate(app, admin, create_employee(admin, "Kavya", "Nair"), ["hr_admin"])
    priya = invite_and_activate(app, admin, create_employee(admin, "Priya", "Shah"), ["employee"])
    return {"admin": admin, "karan": karan, "kavya": kavya, "priya": priya}


def _create(client, name="Pending", list_key="overtime.entries", **extra):
    body = {"list_key": list_key, "name": name, "query": {"status": "PENDING_APPROVAL", "sort": "date"}, **extra}
    return client.post("/api/v1/saved-views", json=body, headers={"Idempotency-Key": uuid.uuid4().hex})


# ------------------------------------------------------------------------------------------- CRUD and limits
def test_create_list_update_delete_own_view(co):
    r = _create(co["karan"], is_default=True)
    assert r.status_code == 201, r.text
    view = r.json()
    listed = co["karan"].get("/api/v1/saved-views", params={"list_key": "overtime.entries"}).json()
    assert [v["name"] for v in listed["views"]] == ["Pending"] and listed["views"][0]["is_default"]
    assert listed["page_sizes"] == [25, 50] and listed["limit"] == 20
    r = co["karan"].patch(f"/api/v1/saved-views/{view['id']}", json={"name": "Waiting"},
                          headers={"If-Match": str(view["row_version"])})
    assert r.status_code == 200 and r.json()["name"] == "Waiting"
    stale = co["karan"].patch(f"/api/v1/saved-views/{view['id']}", json={"name": "Again"},
                              headers={"If-Match": str(view["row_version"])})
    assert stale.status_code == 409 and stale.json()["code"] == "ROW_VERSION_CONFLICT"
    assert co["karan"].delete(f"/api/v1/saved-views/{view['id']}").status_code == 204


def test_twenty_views_per_list_then_limit_reached(co):
    for n in range(20):
        assert _create(co["karan"], name=f"View {n}").status_code == 201
    r = _create(co["karan"], name="One too many")
    assert r.status_code == 422 and r.json()["code"] == "SAVED_VIEW_LIMIT_REACHED"
    assert r.json()["details"] == {"limit": 20}


def test_duplicate_name_is_taken_and_one_default_per_list(co):
    first = _create(co["karan"], name="Mine", is_default=True).json()
    r = _create(co["karan"], name="Mine")
    assert r.status_code == 409 and r.json()["code"] == "SAVED_VIEW_NAME_TAKEN"
    _create(co["karan"], name="Other", is_default=True)
    rows = owner_rows("SELECT name, is_default FROM saved_views WHERE system_key IS NULL ORDER BY id")
    assert rows == [{"name": "Mine", "is_default": False}, {"name": "Other", "is_default": True}]
    assert first["id"]


def test_unknown_list_key_and_disallowed_query_are_refused(co):
    r = _create(co["karan"], list_key="nope.list")
    assert r.status_code == 422 and r.json()["code"] == "INVALID_LIST_KEY"
    r = co["karan"].post("/api/v1/saved-views", json={"list_key": "overtime.entries", "name": "Bad",
                                                      "query": {"salary": 1, "sort": "amount"}})
    assert r.status_code == 422 and r.json()["code"] == "INVALID_QUERY"
    assert sorted(r.json()["details"]["keys"]) == ["salary", "sort"]


# --------------------------------------------------------------------------------------- sharing and X-UI-02
def test_shared_view_is_read_only_for_others(co):
    shared = _create(co["karan"], name="Team pending", is_shared=True).json()
    seen = co["kavya"].get("/api/v1/saved-views", params={"list_key": "overtime.entries"}).json()["views"]
    row = next(v for v in seen if v["id"] == shared["id"])
    assert row["own"] is False and row["owner_name"] and row["allowed_actions"] == []
    r = co["kavya"].patch(f"/api/v1/saved-views/{shared['id']}", json={"name": "Mine now"})
    assert r.status_code == 403
    assert co["kavya"].delete(f"/api/v1/saved-views/{shared['id']}").status_code == 403


def test_x_ui_02_private_or_foreign_view_id_is_404_and_not_applied(app, co):
    private = _create(co["karan"], name="Private").json()
    shared = _create(co["karan"], name="Shared", is_shared=True).json()
    other_admin = signup_company(app, "Elsewhere Co")
    foreign = _create(other_admin, name="Foreign").json()
    for view_id in (private["id"], foreign["id"], 999999, "sys:not_a_view"):
        r = co["kavya"].get("/api/v1/overtime/entries", params={"view_id": view_id})
        assert r.status_code == 404, (view_id, r.text)
    r = co["kavya"].get("/api/v1/overtime/entries", params={"view_id": shared["id"]})
    assert r.status_code == 200 and r.json()["applied_view"] == {"id": shared["id"], "name": "Shared", "system": False}
    # Someone else's private view cannot be changed either; the answer is the unknown-id answer.
    assert co["kavya"].patch(f"/api/v1/saved-views/{private['id']}", json={"name": "x"}).status_code == 404


def test_x_ui_04_stored_view_is_revalidated_when_applied(co):
    """A view whose stored query has keys the list no longer allows is applied without them, never an error."""
    view = _create(co["karan"], name="Old", is_shared=True).json()
    from app.database.db import system_engine
    from sqlalchemy import text

    with system_engine.begin() as conn:
        conn.execute(text("UPDATE saved_views SET query = :q WHERE id = :i"),
                     {"q": '{"sort": "salary", "columns": ["minutes", "coordinates"], "status": "PENDING_APPROVAL"}',
                      "i": view["id"]})
    from app.services import saved_views

    definition = saved_views.list_def("overtime.entries")
    assert saved_views.clean_query(definition, {"sort": "salary", "columns": ["minutes", "coordinates"],
                                                "status": "PENDING_APPROVAL"}, strict=False) == {
        "columns": ["minutes"], "status": "PENDING_APPROVAL"}
    assert co["kavya"].get("/api/v1/overtime/entries", params={"view_id": view["id"]}).status_code == 200


# --------------------------------------------------------------------------------------- system views
def test_system_views_follow_personas_and_can_be_hidden(co):
    priya = co["priya"].get("/api/v1/saved-views", params={"list_key": "overtime.entries"}).json()
    assert [v["id"] for v in priya["system_views"]] == ["sys:my_overtime"]
    kavya = co["kavya"].get("/api/v1/saved-views", params={"list_key": "overtime.entries"}).json()
    assert "sys:waiting_for_me" in [v["id"] for v in kavya["system_views"]]
    r = co["priya"].put("/api/v1/saved-views/system-state", json={"list_key": "overtime.entries",
                                                                   "system_key": "my_overtime", "is_hidden": True})
    assert r.status_code == 200 and r.json()["is_hidden"] is True
    priya = co["priya"].get("/api/v1/saved-views", params={"list_key": "overtime.entries"}).json()
    assert priya["system_views"][0]["is_hidden"] is True and priya["views"] == []
    # Not a persona of hers: 404 like an unknown view.
    r = co["priya"].put("/api/v1/saved-views/system-state", json={"list_key": "overtime.entries",
                                                                   "system_key": "in_my_scope", "is_hidden": True})
    assert r.status_code == 404
    r = co["priya"].get("/api/v1/overtime/entries", params={"view_id": "sys:my_overtime"})
    assert r.status_code == 200 and r.json()["applied_view"]["system"] is True


def test_as_ux_01_system_view_catalogue_equals_x04():
    """One catalogue: every system view of a registered list is the X 04 §2.8 row (key, label, personas)."""
    from app.services.saved_views import LIST_KEYS

    text = X04.read_text(encoding="utf-8").split("**System views catalogue", 1)[1].split("### 2.9", 1)[0]
    rows, current = {}, None
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0].startswith("---") or cells[0] == "List key":
            continue
        if cells[0]:
            current = cells[0].strip("`")
        match = re.match(r"(.+?) \(`sys:([a-z0-9_]+)`\)", cells[1])
        if current and match:
            rows.setdefault(current, {})[match.group(2)] = (match.group(1), frozenset(
                p.strip() for p in cells[2].split(",")))
    for list_key, definition in LIST_KEYS.items():
        ours = {v.key: (v.label, v.personas) for v in definition.system_views}
        assert ours == rows.get(list_key, {}), list_key


# --------------------------------------------------------------------------------------- X-UI-03 search
def test_x_ui_03_search_treats_wildcards_literally(co):
    hits = co["kavya"].get("/api/v1/overtime/entries", params={"q": "%"})
    assert hits.status_code == 200 and hits.json()["total"] == 0
    assert co["kavya"].get("/api/v1/overtime/entries", params={"q": "_"}).json()["total"] == 0
    bad = co["kavya"].get("/api/v1/overtime/entries", params={"sort": "salary"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_SORT"
    ok = co["kavya"].get("/api/v1/overtime/entries", params={"sort": "-employee", "limit": 25})
    assert ok.status_code == 200 and ok.json()["limit"] == 25


# --------------------------------------------------------------------------------------- X-UI-01 bulk scope
def _snapshot(ids, as_of):
    from app.services import saved_views

    return saved_views.list_snapshot(ids, as_of)


def test_x_ui_01_same_count_different_set_is_bulk_scope_changed():
    from app.core.errors import AppError
    from app.services import saved_views

    as_of = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc)
    seen = [11, 12, 13]
    snap = _snapshot(seen, as_of)
    # Unchanged: acts on the same ids.
    scope = saved_views.bulk_scope(filter_query={"status": "PENDING"}, expected_total=3, snapshot=snap,
                                   matching_ids=lambda t: [11, 12, 13])
    assert scope.ids == [11, 12, 13] and scope.skipped == []
    # One withdrawn since: still the set she saw; the withdrawn one is skipped, never acted on.
    scope = saved_views.bulk_scope(filter_query={"status": "PENDING"}, expected_total=3, snapshot=snap,
                                   matching_ids=lambda t: [11, 13], left_since=lambda t: [12])
    assert scope.ids == [11, 13] and scope.skipped == [12]
    # Same count, different members (12 replaced by 14 created before as_of): refused.
    with pytest.raises(AppError) as err:
        saved_views.bulk_scope(filter_query={"status": "PENDING"}, expected_total=3, snapshot=snap,
                               matching_ids=lambda t: [11, 13, 14])
    assert err.value.code == "BULK_SCOPE_CHANGED" and err.value.status_code == 409
    # A changed count is refused too.
    with pytest.raises(AppError) as err:
        saved_views.bulk_scope(filter_query={}, expected_total=3, snapshot=snap, matching_ids=lambda t: [11, 12, 13, 15])
    assert err.value.extra["details"] == {"total": 4, "expected_total": 3}


def test_bulk_scope_explicit_ids_are_capped_at_100():
    from app.core.errors import AppError
    from app.services import saved_views

    assert saved_views.bulk_scope(ids=[3, 3, 4]).ids == [3, 4]
    with pytest.raises(AppError) as err:
        saved_views.bulk_scope(ids=list(range(1, 102)))
    assert err.value.code == "TOO_MANY_ITEMS"
    with pytest.raises(AppError):
        saved_views.bulk_scope(filter_query={}, expected_total=1, snapshot={"as_of": datetime.now(timezone.utc)
                                                                              .isoformat()},
                               matching_ids=lambda t: [1])


# --------------------------------------------------------------------------------------- BUG-AS-55
def test_bug_as_55_audit_list_has_the_standard_envelope(co):
    r = co["admin"].get("/api/v1/audit-logs", params={"skip": 0, "limit": 25})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "skip", "limit"} and body["limit"] == 25
    assert body["items"] == body["logs"]  # alias kept for existing clients
