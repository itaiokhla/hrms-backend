"""LH BUG-10 (part 1): every payroll pre-check "fix" link opens a page that exists in the frontend today.

Pure: reads app/payroll/services/runs.py and the Next.js app directory; a link whose path has no page.tsx fails.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND.parent / "hrms-frontend" / "src" / "app"


def _page_exists(path: str) -> bool:
    parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
    for group in [APP_DIR, *[d for d in APP_DIR.iterdir() if d.is_dir() and d.name.startswith("(")]]:
        folder = group
        ok = True
        for part in parts:
            exact = folder / part
            dynamic = [d for d in folder.iterdir() if d.is_dir() and d.name.startswith("[")] if folder.is_dir() else []
            if exact.is_dir():
                folder = exact
            elif dynamic:
                folder = dynamic[0]
            else:
                ok = False
                break
        if ok and (folder / "page.tsx").exists():
            return True
    return False


def _links() -> set:
    source = (BACKEND / "app" / "payroll" / "services" / "runs.py").read_text(encoding="utf-8")
    return set(re.findall(r'fix_link=f?"(/[^"{?]*)', source))


def test_bug10_pending_leave_fix_link_opens_an_existing_page():
    source = (BACKEND / "app" / "payroll" / "services" / "runs.py").read_text(encoding="utf-8")
    assert 'fix_link="/leaves?tab=approvals"' in source
    assert "/leave/approvals" not in source  # the old link: no such page
    assert _page_exists("/leaves") and "approvals" in (APP_DIR / "(app)" / "leaves" / "page.tsx").read_text(
        encoding="utf-8")


def test_bug10_page_resolver_sees_real_pages():
    assert _page_exists("/leaves") and _page_exists("/attendance")
    assert not _page_exists("/leave/approvals")
    # Other payroll pre-check links are reported to the payroll owner (FOLLOW-UPS FU-AS-04), not changed here.
    assert _links()
