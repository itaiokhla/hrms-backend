"""scripts/ec_coverage.py: a Playwright/Jest test that is skipped or marked fixme asserts nothing, so its EC id must
not count as covered (gate-chrome, 24 Oct: the Chrome suite records unimplemented cases as test.fixme)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import ec_coverage  # noqa: E402


def _titles(source: str) -> list:
    return [title for _, title in ec_coverage.JS_TEST_TITLE.findall(source)]


def test_fixme_and_skip_titles_do_not_count_as_coverage():
    source = """
    test("EC-UX-01 real test", async () => {});
    test.fixme("EC-UX-02 not implemented yet", async () => {});
    test.skip("EC-UX-03 skipped", async () => {});
    it.skip('EC-UX-04 skipped jest', () => {});
    test.describe("EC-UX-05 a group", () => {});
    it(`EC-UX-06 template title`, () => {});
    test.todo("EC-UX-07 later");
    """
    assert _titles(source) == ["EC-UX-01 real test", "EC-UX-05 a group", "EC-UX-06 template title"]


def test_scan_of_a_spec_file_ignores_fixme(tmp_path, monkeypatch):
    spec = tmp_path / "x.spec.ts"
    spec.write_text('test("EC-UX-01 ok", () => {});\ntest.fixme("EC-UX-02 missing", () => {});\n', encoding="utf-8")
    monkeypatch.setattr(ec_coverage, "REPO", tmp_path)
    cov = ec_coverage.Coverage()
    ec_coverage._scan_js(spec, cov, {"EC-UX-01", "EC-UX-02"})
    assert set(cov.hits) == {"EC-UX-01"}
    assert cov.hits["EC-UX-01"][0].level == "frontend-e2e"
