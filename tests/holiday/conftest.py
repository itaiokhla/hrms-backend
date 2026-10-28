"""Holiday test fixtures.

Publishing a holiday year needs a fresh step-up of the session (LH-SEC-02). Tests about calendars, rules, day facts
or imports publish as setup; they get the step-up check switched off. Tests marked ``step_up`` keep it on and prove
the rule itself (tests/holiday/test_holiday_edge_cases.py::test_lh_sec_02_*).
"""

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "step_up: keep the holiday-publish step-up check (LH-SEC-02) on")


@pytest.fixture(autouse=True)
def _holiday_publish_step_up(request, monkeypatch):
    if request.node.get_closest_marker("step_up"):
        yield
        return
    from app.services import holiday_years

    monkeypatch.setattr(holiday_years, "require_publish_step_up", lambda db, principal: None)
    yield
