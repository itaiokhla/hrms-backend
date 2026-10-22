"""The generated frontend catalogues stay in sync with the backend (F-22).

* render() is deterministic and the committed hrms-frontend/src/lib/payroll/catalogues.generated.ts equals
  it (run ``python scripts/export_payroll_catalogues.py`` after changing a vocabulary);
* every state machine and every derived vocabulary (approval requests, overtime entries, wage-input
  imports, loan actions) is labelled exactly with the backend's states;
* the generator refuses a vocabulary with an unlabelled or unknown state.
"""

import importlib.util
import re
from pathlib import Path

import pytest

from app.payroll import state_machines as sm

BACKEND = Path(__file__).resolve().parents[2]
GENERATED = BACKEND.parent / "hrms-frontend" / "src" / "lib" / "payroll" / "catalogues.generated.ts"


def _export_module():
    spec = importlib.util.spec_from_file_location("export_payroll_catalogues",
                                                  BACKEND / "scripts" / "export_payroll_catalogues.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def exporter():
    return _export_module()


def test_render_is_deterministic(exporter):
    assert exporter.render() == exporter.render()


def test_generated_typescript_is_up_to_date(exporter):
    assert GENERATED.exists(), GENERATED
    committed = GENERATED.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == exporter.render(), "run scripts/export_payroll_catalogues.py"


def test_derived_vocabularies_match_the_backend_status_constants(exporter):
    from app.models.approvals import APPROVAL_REQUEST_STATUSES
    from app.models.variable_inputs import IMPORT_STATUSES, LOAN_ACTION_STATUSES
    from app.models.work_items import OVERTIME_STATUSES

    expected = {
        "approval_request": APPROVAL_REQUEST_STATUSES,
        "overtime_entry": OVERTIME_STATUSES,
        "wage_input_import": IMPORT_STATUSES,
        "loan_action": LOAN_ACTION_STATUSES,
    }
    assert set(exporter.DERIVED) == set(expected)
    vocabularies = exporter._vocabularies()
    for key, statuses in expected.items():
        assert set(exporter.DERIVED[key][3]) == set(statuses), key
        vocabulary = vocabularies[key]
        assert vocabulary["machine"] is None
        assert set(vocabulary["states"]) == set(statuses), key
        assert set(vocabulary["initial"]) | set(vocabulary["terminal"]) <= set(statuses), key


def test_every_vocabulary_of_the_inputs_pages_is_exported(exporter):
    """The statuses the inputs pages had to define locally (F-44) are generated now."""
    text = exporter.render()
    names = re.search(r"export type VocabularyName = (.*);", text).group(1)
    for key in ("approval_request", "overtime_entry", "wage_input_import", "loan_action", "task", "wage_input",
                "loan", "claim", "job", "employee_payment"):
        assert f'"{key}"' in names, key
    for key, machine in exporter.VOCABULARY_KEYS.items():
        assert set(exporter._vocabularies()[machine]["states"]) == set(sm.MACHINES[key].states)


def test_generator_refuses_unlabelled_or_unknown_states(exporter, monkeypatch):
    initial, side, terminal, labels = exporter.DERIVED["overtime_entry"]
    broken = dict(labels)
    broken.pop("SUPERSEDED")
    monkeypatch.setitem(exporter.DERIVED, "overtime_entry", (initial, side, terminal, broken))
    with pytest.raises(SystemExit, match="overtime_entry"):
        exporter.render()
    monkeypatch.setitem(exporter.DERIVED, "overtime_entry", (initial, side, terminal, labels))
    bad_tone = {**labels, "APPROVED": ("Approved", "green", "x")}
    monkeypatch.setitem(exporter.DERIVED, "overtime_entry", (initial, side, terminal, bad_tone))
    with pytest.raises(SystemExit, match="bad label entry"):
        exporter.render()
    monkeypatch.setitem(exporter.DERIVED, "overtime_entry", (initial, side, terminal, labels))
    monkeypatch.setitem(exporter.DERIVED, "task", (("DRAFT",), (), (), {"DRAFT": ("Draft", "neutral", "x")}))
    with pytest.raises(SystemExit, match="clash"):
        exporter.render()

