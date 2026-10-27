"""Static scan for readers of attendance sessions and of the legacy status vocabulary (spine §15.4, AS 11 §11.6).

A reader is a function (``path::Class.function``) under ``app/`` that queries the ``Attendance`` model
(``db.query(Attendance ...)``, ``select(Attendance ...)``, a join on it) or compares an attendance status with a
legacy literal (``status == "late"`` and similar) or reads ``STATUS_MAP``. Pure: parses source, imports nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, Set, Tuple

APP = Path(__file__).resolve().parents[2] / "app"
LEGACY_STATUSES = {"present", "late", "half-day", "absent"}
QUERY_CALLS = {"query", "select", "join", "outerjoin"}


def _functions(tree: ast.AST) -> Iterator[Tuple[str, ast.AST]]:
    def walk(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")

    yield from walk(tree, "")


def _queries_attendance(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in QUERY_CALLS:
                for arg in node.args:
                    root = arg
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name) and root.id == "Attendance":
                        return True
    return False


def _reads_status_vocabulary(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and node.id == "STATUS_MAP":
            return True
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            attr = any(isinstance(s, ast.Attribute) and s.attr == "status" for s in sides)
            literal = any(isinstance(s, ast.Constant) and s.value in LEGACY_STATUSES for s in sides)
            if attr and literal:
                return True
    return False


def scan(root: Path = APP) -> Set[str]:
    found: Set[str] = set()
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "Attendance" not in source and "STATUS_MAP" not in source:
            continue
        tree = ast.parse(source)
        rel = path.relative_to(root.parent).as_posix()
        for name, fn in _functions(tree):
            if "Attendance" in source and _queries_attendance(fn):
                found.add(f"{rel}::{name}")
            elif _reads_status_vocabulary(fn) and ("attendance" in rel or "STATUS_MAP" in ast.unparse(fn)):
                found.add(f"{rel}::{name}")
    return found


if __name__ == "__main__":
    for item in sorted(scan()):
        print(item)
