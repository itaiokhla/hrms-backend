"""One linear Alembic chain with short ids (AS 14 §0.5; LH IG-02; AS 11 §11.1 R-9).

Parses alembic/versions without importing Alembic: every revision id is at most 32 characters (the default
``alembic_version.version_num`` width) and starts with its file's number, every ``down_revision`` exists, and the
chain has exactly one head and no branch point.
"""

import ast
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"


def _revisions():
    found = {}
    for path in sorted(VERSIONS.glob("*.py")):
        values = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            target = node.target if isinstance(node, ast.AnnAssign) else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id in ("revision", "down_revision") and node.value is not None:
                values[target.id] = ast.literal_eval(node.value)
        found[path.stem] = values
    return found


def test_as_mig_09_revision_ids_fit_the_32_character_version_column():
    for stem, values in _revisions().items():
        assert values["revision"] == stem, stem
        assert len(stem) <= 32, f"{stem} is longer than 32 characters"


def test_chain_is_linear_with_one_head():
    revisions = _revisions()
    ids = set(revisions)
    parents = [v["down_revision"] for v in revisions.values() if v.get("down_revision")]
    assert all(p in ids for p in parents), "a down_revision names a missing revision"
    assert len(parents) == len(set(parents)), "two revisions share a parent (branch)"
    heads = ids - set(parents)
    assert len(heads) == 1, f"expected one head, found {sorted(heads)}"
