"""AS-M1 shift versions (AS 11 §3.5, §4.4; 03-shifts/01 §2.3, §3; spine §3.4; A-17, A-24, A-31, SP-AS-205/-235).

* ``shifts`` gains ``code`` (backfilled from the name, upper snake, de-duplicated with ``_2``, ``_3``), ``kind``
  (``is_flexible`` -> FLEXIBLE, else FIXED), ``color_token`` (round-robin ``shift-1`` .. ``shift-12`` by name),
  ``legal_entity_id`` / ``branch_id`` (owner scope, NULL = tenant-wide), ``status`` (``is_active`` becomes its
  mirror), ``current_version_id``, ``half_split_time`` (LH V3-030, A-31) and ``row_version``. Trigger
  ``shifts_defaults()`` keeps legacy inserts working: a missing code is derived from the name, ``is_flexible``
  implies a non-FIXED kind, and ``status`` / ``is_active`` follow each other.
* NEW ``shift_versions`` with RLS, grants and composite foreign keys. Triggers: ``shift_versions_no_overlap()``
  (immediate; non-void versions of one shift never overlap, 23P01), ``shift_versions_tiling()`` (deferred
  constraint trigger: from the first version on, no gap and only the last version open-ended, 23514
  ``shift_versions_gap``) and ``shift_versions_guard()`` (A-24: a version that has started on the owner's local
  today is immutable except one shortening of ``effective_to`` to a date >= that today - 1, and a void; 23514
  ``shift_versions_in_force``). The owner zone is branch -> legal entity -> company (``shift_owner_today()``).
  A void restores the previous version's end inside the same transaction with ``app.shift_version_void = on``.
* Data (zero diff, 03-shifts/01 §3.10): version 1 per shift from 2000-01-01 with today's times,
  ``duration_hours`` and ``unpaid_break_minutes``; 24-hour rows get ``long_duty``; ``current_version_id`` points at
  it. Deviation (IG-AS-27): a NULL ``duration_hours`` (BUG-AS-23 rows) stays NULL instead of being computed, so
  payroll standard hours stay identical; the catalogue service computes it on every new version.
* ``source_change_log.record_type`` accepts ``shift_version`` and ``shift_rule``.

Downgrade refuses while a shift has more than one version (AS 11 §11.1 M-5: export first), then drops the table,
triggers and columns.

Revision ID: 0040_as_m1_shift_versions
Revises: 0039_lh_m1_scheduler
Create Date: 2026-10-27
"""
import hashlib
import json
import re
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_as_m1_shift_versions"
down_revision: Union[str, None] = "0039_lh_m1_scheduler"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies: later model changes must not alter what this revision does.
TENANT_PREDICATE = "company_id = NULLIF(current_setting('app.company_id', true), '')::integer"
TABLE = "shift_versions"
SHIFT_KINDS = ("FIXED", "FLEXIBLE", "OPEN", "SPLIT")
FIRST_DATE = "2000-01-01"
RECORD_TYPES_BEFORE = (
    "attendance", "leave", "holiday", "lop_input", "suspension", "on_duty", "optional_holiday",
    "overtime", "task", "wage_input", "assignment", "work_week_version", "calendar_link",
)
RECORD_TYPES_AFTER = RECORD_TYPES_BEFORE + ("shift_version", "shift_rule")
CONTENT_FIELDS = (
    "start_time", "end_time", "crosses_midnight", "duration_hours", "unpaid_break_minutes", "half_split_time",
    "segments", "break_rules", "core_start_time", "core_end_time", "expected_net_minutes", "grace_in_minutes",
    "grace_out_minutes", "early_in_window_minutes", "late_out_window_minutes", "anchor_timezone", "safety_critical",
    "outdoor", "long_duty",
)


def _hash(values: dict) -> str:
    content = {}
    for field in CONTENT_FIELDS:
        value = values.get(field)
        if isinstance(value, Decimal):
            value = f"{value:.2f}"
        content[field] = value
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _code(name: str, taken: set) -> str:
    base = re.sub(r"[^A-Z0-9]+", "_", (name or "").upper()).strip("_") or "SHIFT"
    base = re.sub(r"^[^A-Z0-9]+", "", base)[:20] or "SHIFT"
    code, n = base, 1
    while code in taken:
        n += 1
        suffix = f"_{n}"
        code = base[: 20 - len(suffix)] + suffix
    taken.add(code)
    return code


def _fk(columns, target, name):
    local = ["company_id", *([columns] if isinstance(columns, str) else columns)]
    return sa.ForeignKeyConstraint(local, [f"{target}.company_id", f"{target}.id"], name=f"fk_{TABLE}_{name}")


SHIFTS_DEFAULTS = r"""
CREATE OR REPLACE FUNCTION shifts_defaults() RETURNS trigger AS $$
DECLARE
    base text;
    candidate text;
    n integer := 1;
BEGIN
    IF TG_OP = 'INSERT' AND NEW.code IS NULL THEN
        base := left(coalesce(nullif(btrim(regexp_replace(upper(coalesce(NEW.name, '')), '[^A-Z0-9]+', '_', 'g'), '_'), ''),
                              'SHIFT'), 20);
        candidate := base;
        WHILE EXISTS (SELECT 1 FROM shifts s WHERE s.company_id = NEW.company_id AND s.code = candidate) LOOP
            n := n + 1;
            candidate := left(base, 20 - length('_' || n)) || '_' || n;
        END LOOP;
        NEW.code := candidate;
    END IF;
    IF NEW.is_flexible AND NEW.kind = 'FIXED' THEN
        NEW.kind := 'FLEXIBLE';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.is_active := (NEW.status = 'ACTIVE');
    ELSIF TG_OP = 'UPDATE' AND NEW.is_active IS DISTINCT FROM OLD.is_active THEN
        NEW.status := CASE WHEN NEW.is_active THEN 'ACTIVE' ELSE 'ARCHIVED' END;
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.status IS NULL OR (NEW.status = 'ACTIVE' AND NOT NEW.is_active) THEN
            NEW.status := CASE WHEN NEW.is_active THEN 'ACTIVE' ELSE 'ARCHIVED' END;
        END IF;
        NEW.is_active := (NEW.status = 'ACTIVE');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

OWNER_TODAY = r"""
CREATE OR REPLACE FUNCTION shift_owner_today(p_company_id integer, p_shift_id integer) RETURNS date AS $$
    SELECT (now() AT TIME ZONE coalesce(b.timezone, le.timezone, c.timezone))::date
    FROM shifts s
    JOIN companies c ON c.id = s.company_id
    LEFT JOIN branches b ON b.company_id = s.company_id AND b.id = s.branch_id
    LEFT JOIN legal_entities le ON le.company_id = s.company_id AND le.id = coalesce(s.legal_entity_id, b.legal_entity_id)
    WHERE s.company_id = p_company_id AND s.id = p_shift_id
$$ LANGUAGE sql STABLE;
"""

NO_OVERLAP = r"""
CREATE OR REPLACE FUNCTION shift_versions_no_overlap() RETURNS trigger AS $$
BEGIN
    IF NEW.voided_at IS NOT NULL THEN
        RETURN NEW;
    END IF;
    -- Serialise version writes per shift, then reject overlapping non-void windows.
    PERFORM 1 FROM shifts WHERE company_id = NEW.company_id AND id = NEW.shift_id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM shift_versions v
        WHERE v.company_id = NEW.company_id AND v.shift_id = NEW.shift_id AND v.id <> NEW.id AND v.voided_at IS NULL
          AND daterange(v.effective_from, v.effective_to, '[]') && daterange(NEW.effective_from, NEW.effective_to, '[]')
    ) THEN
        RAISE EXCEPTION 'Versions of one shift cannot overlap'
            USING ERRCODE = 'exclusion_violation', CONSTRAINT = 'shift_versions_no_overlap';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TILING = r"""
CREATE OR REPLACE FUNCTION shift_versions_tiling() RETURNS trigger AS $$
DECLARE
    row_ record;
    previous_to date;
    first boolean := true;
    open_seen boolean := false;
    target_company integer := coalesce(NEW.company_id, OLD.company_id);
    target_shift integer := coalesce(NEW.shift_id, OLD.shift_id);
BEGIN
    FOR row_ IN
        SELECT effective_from, effective_to FROM shift_versions
        WHERE company_id = target_company AND shift_id = target_shift AND voided_at IS NULL
        ORDER BY effective_from
    LOOP
        IF open_seen OR (NOT first AND row_.effective_from <> previous_to + 1) THEN
            RAISE EXCEPTION 'Versions of one shift must follow each other without a gap'
                USING ERRCODE = 'check_violation', CONSTRAINT = 'shift_versions_gap';
        END IF;
        first := false;
        previous_to := row_.effective_to;
        open_seen := row_.effective_to IS NULL;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

GUARD = r"""
CREATE OR REPLACE FUNCTION shift_versions_guard() RETURNS trigger AS $$
DECLARE
    today date := shift_owner_today(OLD.company_id, OLD.shift_id);
    voiding boolean := coalesce(current_setting('app.shift_version_void', true), '') = 'on';
BEGIN
    IF today IS NULL OR OLD.effective_from > today THEN
        RETURN coalesce(NEW, OLD);  -- a scheduled version may be replaced or removed
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'A shift version in force cannot be removed'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'shift_versions_in_force';
    END IF;
    IF (to_jsonb(NEW) - ARRAY['effective_to', 'voided_at', 'voided_by_user_id', 'updated_at'])
        IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['effective_to', 'voided_at', 'voided_by_user_id', 'updated_at']) THEN
        RAISE EXCEPTION 'A shift version in force is immutable'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'shift_versions_in_force';
    END IF;
    IF OLD.voided_at IS NOT NULL AND NEW.voided_at IS DISTINCT FROM OLD.voided_at THEN
        RAISE EXCEPTION 'A void cannot be undone'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'shift_versions_in_force';
    END IF;
    IF NEW.effective_to IS DISTINCT FROM OLD.effective_to AND NOT voiding AND (
        NEW.effective_to IS NULL OR NEW.effective_to < today - 1
        OR (OLD.effective_to IS NOT NULL AND NEW.effective_to > OLD.effective_to)) THEN
        RAISE EXCEPTION 'A shift version in force may only be ended from yesterday on'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'shift_versions_in_force';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("shifts", sa.Column("code", sa.String(20), nullable=True))
    op.add_column("shifts", sa.Column("kind", sa.String(10), nullable=False, server_default="FIXED"))
    op.add_column("shifts", sa.Column("color_token", sa.String(10), nullable=False, server_default="shift-1"))
    op.add_column("shifts", sa.Column("legal_entity_id", sa.Integer(), nullable=True))
    op.add_column("shifts", sa.Column("branch_id", sa.Integer(), nullable=True))
    op.add_column("shifts", sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"))
    op.add_column("shifts", sa.Column("current_version_id", sa.Integer(), nullable=True))
    op.add_column("shifts", sa.Column("half_split_time", sa.String(5), nullable=True))
    op.add_column("shifts", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("shift_id", sa.Integer(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("start_time", sa.String(5), nullable=True),
        sa.Column("end_time", sa.String(5), nullable=True),
        sa.Column("crosses_midnight", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("duration_hours", sa.Numeric(4, 2), nullable=True),
        sa.Column("unpaid_break_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("half_split_time", sa.String(5), nullable=True),
        sa.Column("segments", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("break_rules", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("core_start_time", sa.String(5), nullable=True),
        sa.Column("core_end_time", sa.String(5), nullable=True),
        sa.Column("expected_net_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("grace_in_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("grace_out_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("early_in_window_minutes", sa.SmallInteger(), nullable=False, server_default="120"),
        sa.Column("late_out_window_minutes", sa.SmallInteger(), nullable=False, server_default="240"),
        sa.Column("anchor_timezone", sa.String(64), nullable=True),
        sa.Column("safety_critical", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("outdoor", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("long_duty", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), nullable=True),
        sa.UniqueConstraint("company_id", "id", name=f"uq_{TABLE}_company_id_id"),
        sa.UniqueConstraint("company_id", "shift_id", "version_no", name=f"uq_{TABLE}_version"),
        _fk("shift_id", "shifts", "shift"),
        _fk("created_by_user_id", "users", "created_by"),
        _fk("voided_by_user_id", "users", "voided_by"),
        sa.CheckConstraint("version_no >= 1", name=f"ck_{TABLE}_version_no"),
        sa.CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name=f"ck_{TABLE}_dates"),
        sa.CheckConstraint("start_time IS NULL OR end_time IS NULL OR crosses_midnight = (end_time <= start_time)",
                           name=f"ck_{TABLE}_crosses_midnight"),
        sa.CheckConstraint("duration_hours IS NULL OR (duration_hours > 0 AND duration_hours <= 24)",
                           name=f"ck_{TABLE}_duration"),
        sa.CheckConstraint("unpaid_break_minutes IS NULL OR unpaid_break_minutes BETWEEN 0 AND 1440",
                           name=f"ck_{TABLE}_unpaid_break"),
        sa.CheckConstraint("jsonb_typeof(segments) = 'array' AND jsonb_array_length(segments) <= 4",
                           name=f"ck_{TABLE}_segments"),
        sa.CheckConstraint("jsonb_typeof(break_rules) = 'array' AND jsonb_array_length(break_rules) <= 4",
                           name=f"ck_{TABLE}_break_rules"),
        sa.CheckConstraint("expected_net_minutes IS NULL OR expected_net_minutes BETWEEN 1 AND 1440",
                           name=f"ck_{TABLE}_expected_net"),
        sa.CheckConstraint("(grace_in_minutes IS NULL OR grace_in_minutes BETWEEN 0 AND 240) AND "
                           "(grace_out_minutes IS NULL OR grace_out_minutes BETWEEN 0 AND 240)", name=f"ck_{TABLE}_grace"),
        sa.CheckConstraint("early_in_window_minutes BETWEEN 0 AND 720 AND late_out_window_minutes BETWEEN 0 AND 720",
                           name=f"ck_{TABLE}_windows"),
        sa.CheckConstraint("NOT long_duty OR start_time = end_time", name=f"ck_{TABLE}_long_duty"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name=f"ck_{TABLE}_hash"),
        sa.CheckConstraint("(voided_at IS NULL) = (voided_by_user_id IS NULL)", name=f"ck_{TABLE}_void"),
    )
    op.create_index(f"ix_{TABLE}_company_id", TABLE, ["company_id"])
    op.create_index(f"ix_{TABLE}_shift_id", TABLE, ["shift_id"])
    op.create_index(f"ix_{TABLE}_shift_dates", TABLE, ["company_id", "shift_id", "effective_from"])
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON {TABLE} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")
    roles = [r for (r,) in bind.execute(sa.text(
        "SELECT DISTINCT grantee FROM information_schema.role_table_grants "
        "WHERE table_name = 'employees' AND privilege_type = 'INSERT' AND grantee <> current_user"
    ))]
    for role in roles:
        quoted = '"' + role.replace('"', '""') + '"'
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {quoted}")
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {TABLE}_id_seq TO {quoted}")

    # Backfill (zero diff): codes, kinds, colours, then version 1 of every shift.
    rows = bind.execute(sa.text(
        "SELECT id, company_id, name, start_time, end_time, duration_hours, unpaid_break_minutes, is_flexible, "
        "is_active FROM shifts ORDER BY company_id, lower(name), id")).mappings().all()
    taken: dict = {}
    position: dict = {}
    for row in rows:
        cid = row["company_id"]
        code = _code(row["name"], taken.setdefault(cid, set()))
        position[cid] = position.get(cid, 0) + 1
        color = f"shift-{(position[cid] - 1) % 12 + 1}"
        start, end = row["start_time"], row["end_time"]
        crosses = _minutes(end) <= _minutes(start)
        long_duty = start == end
        # Kept as stored, NULL included: a computed value would give BUG-AS-23 shifts standard hours they do not
        # have today (payroll zero diff wins, IG-AS-27); new saves compute it.
        duration = row["duration_hours"]
        values = {
            "start_time": start, "end_time": end, "crosses_midnight": crosses, "duration_hours": duration,
            "unpaid_break_minutes": row["unpaid_break_minutes"], "half_split_time": None, "segments": [],
            "break_rules": [], "core_start_time": None, "core_end_time": None, "expected_net_minutes": None,
            "grace_in_minutes": None, "grace_out_minutes": None, "early_in_window_minutes": 120,
            "late_out_window_minutes": 240, "anchor_timezone": None, "safety_critical": False, "outdoor": False,
            "long_duty": long_duty,
        }
        version_id = bind.execute(sa.text(
            f"INSERT INTO {TABLE} (company_id, shift_id, version_no, effective_from, start_time, end_time, "
            "crosses_midnight, duration_hours, unpaid_break_minutes, long_duty, content_hash, reason) "
            "VALUES (:cid, :sid, 1, DATE :first, :start, :end, :crosses, :duration, :unpaid, :long_duty, :hash, "
            "'Migrated: the shift as it was before versions') RETURNING id"),
            {"cid": cid, "sid": row["id"], "first": FIRST_DATE, "start": start, "end": end, "crosses": crosses,
             "duration": duration, "unpaid": row["unpaid_break_minutes"], "long_duty": long_duty,
             "hash": _hash(values)}).scalar()
        bind.execute(sa.text(
            "UPDATE shifts SET code = :code, kind = :kind, color_token = :color, current_version_id = :vid, "
            "status = CASE WHEN is_active THEN 'ACTIVE' ELSE 'ARCHIVED' END WHERE id = :sid"),
            {"code": code, "kind": "FLEXIBLE" if row["is_flexible"] else "FIXED", "color": color, "vid": version_id,
             "sid": row["id"]})

    op.alter_column("shifts", "code", nullable=False, server_default=None)
    op.create_unique_constraint("uq_shifts_company_code", "shifts", ["company_id", "code"])
    op.create_foreign_key("fk_shifts_legal_entity", "shifts", "legal_entities", ["company_id", "legal_entity_id"],
                          ["company_id", "id"])
    op.create_foreign_key("fk_shifts_branch", "shifts", "branches", ["company_id", "branch_id"], ["company_id", "id"])
    op.create_foreign_key("fk_shifts_current_version", "shifts", TABLE, ["company_id", "current_version_id"],
                          ["company_id", "id"])
    op.create_check_constraint("ck_shifts_code", "shifts", "code ~ '^[A-Z0-9][A-Z0-9_-]{0,19}$'")
    op.create_check_constraint("ck_shifts_kind", "shifts", f"kind IN {SHIFT_KINDS}")
    op.create_check_constraint("ck_shifts_color_token", "shifts", "color_token ~ '^shift-([1-9]|1[0-2])$'")
    op.create_check_constraint("ck_shifts_status", "shifts", "status IN ('ACTIVE', 'ARCHIVED')")
    op.create_check_constraint("ck_shifts_is_active", "shifts", "is_active = (status = 'ACTIVE')")
    op.create_check_constraint("ck_shifts_row_version", "shifts", "row_version >= 1")

    op.execute(SHIFTS_DEFAULTS)
    op.execute("CREATE TRIGGER trg_shifts_defaults BEFORE INSERT OR UPDATE ON shifts "
               "FOR EACH ROW EXECUTE FUNCTION shifts_defaults()")
    op.execute(OWNER_TODAY)
    op.execute(NO_OVERLAP)
    op.execute(f"CREATE TRIGGER trg_{TABLE}_no_overlap BEFORE INSERT OR UPDATE ON {TABLE} "
               "FOR EACH ROW EXECUTE FUNCTION shift_versions_no_overlap()")
    op.execute(TILING)
    op.execute(f"CREATE CONSTRAINT TRIGGER shift_versions_tiling AFTER INSERT OR UPDATE OR DELETE ON {TABLE} "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION shift_versions_tiling()")
    op.execute(GUARD)
    op.execute(f"CREATE TRIGGER trg_{TABLE}_guard BEFORE UPDATE OR DELETE ON {TABLE} "
               "FOR EACH ROW EXECUTE FUNCTION shift_versions_guard()")

    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {RECORD_TYPES_AFTER}")


def downgrade() -> None:
    bind = op.get_bind()
    versioned = bind.execute(sa.text(
        f"SELECT count(*) FROM (SELECT shift_id FROM {TABLE} GROUP BY company_id, shift_id HAVING count(*) > 1) x"
    )).scalar()
    if versioned:
        raise RuntimeError(f"{versioned} shift(s) have more than one version; export them before downgrading")
    used = bind.execute(sa.text(
        "SELECT count(*) FROM source_change_log WHERE record_type IN ('shift_version', 'shift_rule')")).scalar()
    if used:
        raise RuntimeError("source_change_log holds shift_version / shift_rule rows; export them before downgrading")
    op.drop_constraint("ck_source_change_log_record_type", "source_change_log", type_="check")
    op.create_check_constraint("ck_source_change_log_record_type", "source_change_log",
                               f"record_type IN {RECORD_TYPES_BEFORE}")
    op.execute("DROP TRIGGER IF EXISTS trg_shifts_defaults ON shifts")
    op.execute("DROP FUNCTION IF EXISTS shifts_defaults()")
    op.drop_constraint("fk_shifts_current_version", "shifts", type_="foreignkey")
    op.drop_table(TABLE)
    op.execute("DROP FUNCTION IF EXISTS shift_versions_guard()")
    op.execute("DROP FUNCTION IF EXISTS shift_versions_tiling()")
    op.execute("DROP FUNCTION IF EXISTS shift_versions_no_overlap()")
    op.execute("DROP FUNCTION IF EXISTS shift_owner_today(integer, integer)")
    for name in ("ck_shifts_row_version", "ck_shifts_is_active", "ck_shifts_status", "ck_shifts_color_token",
                 "ck_shifts_kind", "ck_shifts_code"):
        op.drop_constraint(name, "shifts", type_="check")
    op.drop_constraint("fk_shifts_branch", "shifts", type_="foreignkey")
    op.drop_constraint("fk_shifts_legal_entity", "shifts", type_="foreignkey")
    op.drop_constraint("uq_shifts_company_code", "shifts", type_="unique")
    for column in ("row_version", "half_split_time", "current_version_id", "status", "branch_id", "legal_entity_id",
                   "color_token", "kind", "code"):
        op.drop_column("shifts", column)
