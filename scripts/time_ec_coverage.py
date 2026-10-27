"""Edge-case coverage for the leave-holiday (LH-*) and attendance-shift / cross-module (AS-*, X-*) sets
(AS 14 IG-AS-07, LH 14 IG-08). Shared by scripts/as_ec_coverage.py and scripts/lh_ec_coverage.py.

A test covers an id when the id is in its name (``test_as_shf_03_...`` -> AS-SHF-03, ``test_lh_sec_07_...`` ->
LH-SEC-07) or in its docstring (the function's or its class's); a Jest / Playwright test when the id is in the title of
a ``test(...)`` / ``it(...)`` / ``describe(...)`` call (skipped and fixme tests never count). The ids a milestone must
cover come from the owner docs: AS 14 §0.6 (AS / X milestones) and LH 13 §4.x "Edge cases that must pass".

Pure static scan, no application imports:

    python scripts/as_ec_coverage.py [--check --milestone AS-M0]
    python scripts/lh_ec_coverage.py [--check --milestone LH-M0]
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
FRONTEND = REPO / "hrms-frontend"

ID_IN_TEXT = re.compile(r"\b(AS|X|LH)-([A-Z]{2,4})-(\d{2})\b")
ID_IN_NAME = re.compile(r"(?:^|_)(as|x|lh)_([a-z]{2,4})_(\d{2})(?=_|$)")
JS_TITLE = re.compile(r"""\b(?:test|it|describe)(?:\.(?!fixme\b|skip\b|todo\b)\w+)?\(\s*(['"`])(.*?)\1""", re.S)
JS_DIRS = ("src", "e2e", "e2e-as", "e2e-lh")


@dataclass(frozen=True)
class SetSpec:
    name: str
    prefixes: tuple
    catalogue: Path
    output: Path


AS_SET = SetSpec("attendance-shift", ("AS", "X"), REPO / "docs" / "attendance-shift" / "edge-cases.json",
                 REPO / "docs" / "attendance-shift" / "implementation" / "EC-COVERAGE.md")
LH_SET = SetSpec("leave-holiday", ("LH",), REPO / "docs" / "leave-holiday" / "edge-cases.json",
                 REPO / "docs" / "leave-holiday" / "implementation" / "EC-COVERAGE.md")


def _norm(prefix: str, area: str, number: str) -> str:
    return f"{prefix.upper()}-{area.upper()}-{number}"


def ids_in_text(text: Optional[str]) -> Set[str]:
    return {_norm(*m) for m in ID_IN_TEXT.findall(text or "")}


def ids_in_name(name: str) -> Set[str]:
    return {_norm(*m) for m in ID_IN_NAME.findall(name.lower())}


def scan() -> Dict[str, Set[str]]:
    """id -> test locations (``path::name``)."""
    found: Dict[str, Set[str]] = defaultdict(set)
    for path in sorted((BACKEND / "tests").rglob("test_*.py")):
        rel = path.relative_to(REPO).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        def visit(node, class_doc: str = ""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    visit(child, ast.get_docstring(child) or "")
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                    ids = ids_in_name(child.name) | ids_in_text(ast.get_docstring(child)) | ids_in_text(class_doc)
                    for case in ids:
                        found[case].add(f"{rel}::{child.name}")

        visit(tree)
    for folder in JS_DIRS:
        root = FRONTEND / folder
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".ts", ".tsx") or "node_modules" in path.parts:
                continue
            if not (path.name.endswith((".test.ts", ".test.tsx", ".spec.ts"))):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for _quote, title in JS_TITLE.findall(text):
                for case in ids_in_text(title):
                    found[case].add(f"{path.relative_to(REPO).as_posix()}::{title[:60]}")
    return found


# ------------------------------------------------------------------------------------------- milestone lists
def _expand(area: str, spec: str) -> List[str]:
    """'01..05, -08..-13, 15' or '01–05' (numbers of one area) -> ids."""
    ids = []
    for part in re.split(r"[,;]", spec):
        part = re.sub(r"\(.*?\)", "", part).strip().lstrip("-").strip()
        if not part:
            continue
        nums = re.findall(r"\d{2}", part)
        if not nums:
            continue
        if len(nums) >= 2 and re.search(r"\.\.|–|-", part):
            ids += [f"{area}-{n:02d}" for n in range(int(nums[0]), int(nums[-1]) + 1)]
        else:
            ids += [f"{area}-{n}" for n in nums]
    return ids


def as_milestone_ids(milestone: str) -> List[str]:
    guide = (REPO / "docs" / "attendance-shift" / "14-developer-implementation-guide.md").read_text(encoding="utf-8")
    section = guide.split("### 0.6", 1)[1].split("\n## ", 1)[0]
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].split(" ")[0] == milestone:
            ids: List[str] = []
            for chunk in cells[1].split(";"):
                chunk = chunk.strip()
                head = re.match(r"((?:AS|X)-[A-Z]{2,4})-(\d{2})(.*)", chunk)
                if not head:
                    continue
                area, first, rest = head.groups()
                rest = rest.replace(f"{area}-", "")
                ids += _expand(area, first + rest)
            return sorted(set(ids))
    raise SystemExit(f"{milestone} is not in AS 14 §0.6")


def lh_milestone_ids(milestone: str) -> List[str]:
    plan = (REPO / "docs" / "leave-holiday" / "13-implementation-plan.md").read_text(encoding="utf-8")
    sections = re.split(r"\n### 4\.\d+ ", plan)
    section = next((s for s in sections if s.startswith(f"{milestone} ")), None)
    if section is None:
        raise SystemExit(f"{milestone} is not in LH 13 §4")
    line = next((ln for ln in section.splitlines() if ln.startswith("**Edge cases that must pass")), "")
    body = re.sub(r"\([^)]*\)", "", line.split(".**", 1)[-1])  # "(doc-FAIL: ...)" notes name no extra cases
    ids: List[str] = []
    for area, numbers in re.findall(r"`(LH-[A-Z]{2,4})-\*`\s*([^·`]*)", body):
        ids += _expand(area, numbers)
    return sorted(set(ids))


# ------------------------------------------------------------------------------------------------- report
def render(spec: SetSpec, cases: list, found: Dict[str, Set[str]], milestone_lists: Dict[str, List[str]]) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    known = {c["id"] for c in cases}
    covered = sorted(i for i in known if found.get(i))
    lines = [f"# Edge-case coverage — {spec.name}", "",
             f"Generated {stamp} by `scripts/{'as' if 'AS' in spec.prefixes else 'lh'}_ec_coverage.py` (static scan of "
             "test names, docstrings and Jest / Playwright titles). Do not edit by hand.", "",
             f"**{len(covered)} / {len(known)}** catalogue cases are named by at least one test.", ""]
    for milestone, ids in milestone_lists.items():
        missing = [i for i in ids if not found.get(i)]
        lines += [f"## {milestone}: {len(ids) - len(missing)} / {len(ids)} covered", "",
                  "| Case | Tests |", "|---|---|"]
        for case in ids:
            tests = sorted(found.get(case, ()))
            lines.append(f"| {case} | {'<br>'.join(f'`{t}`' for t in tests) if tests else '**NOT COVERED**'} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def run(spec: SetSpec, milestone_ids, argv: Optional[Iterable[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--milestone", action="append", default=[], help="milestone(s) to list (e.g. AS-M0)")
    parser.add_argument("--check", action="store_true", help="exit 1 when a case of --milestone is not covered")
    args = parser.parse_args(list(argv) if argv is not None else None)
    cases = json.loads(spec.catalogue.read_text(encoding="utf-8"))
    found = scan()
    lists = {m: milestone_ids(m) for m in args.milestone}
    spec.output.parent.mkdir(parents=True, exist_ok=True)
    spec.output.write_text(render(spec, cases, found, lists), encoding="utf-8")
    missing = {m: [i for i in ids if not found.get(i)] for m, ids in lists.items()}
    for m, ids in lists.items():
        print(f"{m}: {len(ids) - len(missing[m])}/{len(ids)} covered" + (f"; missing {missing[m]}" if missing[m] else ""))
    print(f"wrote {spec.output.relative_to(REPO)}")
    return 1 if args.check and any(missing.values()) else 0
