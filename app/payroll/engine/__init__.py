"""Payroll calculation engine: a pure function of a frozen snapshot (3.5 §7-§8; ARCHITECTURE §4.8).

* ``calculate(snapshot) -> CalcResult`` — lines, totals, exceptions, flags, payment split and
  ``calc_hash`` for one run employee. ``snapshot`` is an ``engine.snapshot.Snapshot`` or its JSON form.
* ``replay(snapshot, expected_hash) -> (matches, CalcResult)`` — recalculates and compares ``calc_hash``.
* ``ENGINE_VERSION`` — stored with every run; part of ``calc_hash``.

Submodules: ``snapshot`` (schema, canonical JSON, content hash), ``context`` (stage 1), ``pipeline``
(stage order), ``earnings`` (stage 4), ``statutory`` (5-7) and ``deductions`` (8) from the country-pack
work, ``net`` (9-11), ``validate`` (12), ``trace`` (explanations and hashing), ``lines`` (shared types).
"""

from app.payroll.engine.lines import CalcIssue, Line
from app.payroll.engine.pipeline import ENGINE_VERSION, CalcResult, calculate, replay
from app.payroll.engine.snapshot import Snapshot, SnapshotError, canonical_json, content_hash

__all__ = [
    "CalcIssue", "CalcResult", "ENGINE_VERSION", "Line", "Snapshot", "SnapshotError", "calculate", "canonical_json",
    "content_hash", "replay",
]
