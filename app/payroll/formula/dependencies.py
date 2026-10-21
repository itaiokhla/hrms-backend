"""Dependency resolution and evaluation order of a salary structure (3.3 §5).

``dependency_order(deps, stage_of)`` returns every component in the order it must
be calculated: grouped by pipeline stage (earnings 4, pre-tax deductions 5,
statutory 6, tax 7, post-tax deductions 8, net 9, employer cost 10; 3.5 §7) and
topologically sorted inside each stage, with ties broken by (stage, code) so the
same structure always gives the same order.

``deps`` maps a component to the codes it reads. Build it from
``Analysis.dependencies``, which already expands ``SUM_TAG``/``SUM_TAG_RATE`` to
every component of the structure carrying the tag; ``expand_tags`` does the same
for callers that hold raw tag references. A balancing component depends on every
other CTC component (``FIXED_ALLOWANCE`` reads ``BASIC`` and ``HRA``).

Problems are raised as FormulaError, all checked before any ordering:

* UNKNOWN_REFERENCE: a dependency with no stage (it is not in the structure);
* STAGE_VIOLATION: a component reads one of a later stage, ``path`` is
  ``[component, dependency]``; the net-to-gross solver (3.3 §6.3) is the only
  sanctioned exception and runs outside this graph;
* CYCLE_DETECTED: ``path`` is the full cycle, first node repeated at the end, as
  in ``["HRA", "FIXED_ALLOWANCE", "HRA"]`` (Example C, EC-CMP-01). The cycle is
  the shortest one through ``focus`` (the component being saved) when ``focus``
  lies on a cycle, otherwise the shortest through the first component of
  ``deps`` (iteration order, i.e. structure line order) that does.
"""

from __future__ import annotations

import heapq
from collections import deque
from typing import Collection, Iterable, Mapping

from app.payroll.formula.errors import ErrorCode, FormulaError

ARROW = " → "


def expand_tags(
    components: Mapping[str, Collection[str]],
    tags_read: Mapping[str, Collection[str]],
    tag_members: Mapping[str, Collection[str]],
) -> dict[str, set[str]]:
    """Merge direct component references with the members of every tag each component sums.

    ``components``: code → components read directly; ``tags_read``: code → tags read
    with SUM_TAG/SUM_TAG_RATE; ``tag_members``: tag → structure components carrying it.
    A component that sums a tag it carries depends on itself, which ``dependency_order``
    reports as the cycle ``[X, X]``.
    """
    result: dict[str, set[str]] = {}
    for code in [*components, *(c for c in tags_read if c not in components)]:
        found = set(components.get(code, ()))
        for tag in tags_read.get(code, ()):
            if tag not in tag_members:
                raise FormulaError(ErrorCode.UNKNOWN_REFERENCE, f"{code} reads unknown tag {tag}", path=[code])
            found.update(tag_members[tag])
        result[code] = found
    return result


def dependency_order(
    deps: Mapping[str, Collection[str]],
    stage_of: Mapping[str, int],
    *,
    focus: str | None = None,
) -> list[str]:
    """Calculation order of every code in ``deps`` or ``stage_of`` (see the module docstring)."""
    graph: dict[str, tuple[str, ...]] = {code: tuple(sorted(set(reads))) for code, reads in deps.items()}
    nodes = set(graph) | set(stage_of) | {d for reads in graph.values() for d in reads}
    for code in sorted(nodes):
        stage = stage_of.get(code)
        if stage is None:
            reader = next((c for c in sorted(graph) if code in graph[c]), None)
            path = [reader, code] if reader is not None else [code]
            raise FormulaError(
                ErrorCode.UNKNOWN_REFERENCE,
                f"{code} is not a component of this structure" + (f" (read by {reader})" if reader else ""),
                path=path,
            )
        if isinstance(stage, bool) or not isinstance(stage, int):
            raise TypeError(f"stage_of[{code!r}] must be an int")
    for code in sorted(graph, key=lambda c: (stage_of[c], c)):
        for dep in graph[code]:
            if stage_of[dep] > stage_of[code]:
                raise FormulaError(
                    ErrorCode.STAGE_VIOLATION,
                    f"{code} (stage {stage_of[code]}) reads {dep}, which is calculated later (stage {stage_of[dep]}); "
                    "a component may only use components of its own or earlier stages",
                    path=[code, dep],
                )

    waiting = {code: len(graph.get(code, ())) for code in nodes}
    readers: dict[str, list[str]] = {code: [] for code in nodes}
    for code, reads in graph.items():
        for dep in reads:
            readers[dep].append(code)
    ready = [(stage_of[code], code) for code in nodes if waiting[code] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, code = heapq.heappop(ready)
        order.append(code)
        for reader in readers[code]:
            waiting[reader] -= 1
            if waiting[reader] == 0:
                heapq.heappush(ready, (stage_of[reader], reader))
    if len(order) < len(nodes):
        blocked = {code for code in nodes if waiting[code] > 0}
        candidates = [focus] if focus is not None else []
        candidates += [code for code in graph if code in blocked]
        path = _cycle(graph, blocked, candidates)
        raise FormulaError(ErrorCode.CYCLE_DETECTED, f"circular dependency: {ARROW.join(path)}", path=path)
    return order


def _cycle(graph: Mapping[str, tuple[str, ...]], blocked: set[str], candidates: Iterable[str]) -> list[str]:
    """Shortest cycle through the first candidate that lies on one (breadth-first, sorted edges)."""
    for start in candidates:
        if start not in blocked:
            continue
        parent: dict[str, str] = {}
        queue: deque[str] = deque([start])
        seen = {start}
        while queue:
            current = queue.popleft()
            for dep in graph.get(current, ()):
                if dep == start:
                    path = [current]
                    while path[-1] != start:
                        path.append(parent[path[-1]])
                    return [start, *reversed(path[:-1]), start] if current != start else [start, start]
                if dep in blocked and dep not in seen:
                    seen.add(dep)
                    parent[dep] = current
                    queue.append(dep)
    raise AssertionError("blocked components must contain a cycle")  # pragma: no cover
