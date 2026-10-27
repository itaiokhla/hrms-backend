"""JC-AS-15: job handlers outside app/payroll/services load from an explicit module list (WI-AS-0.4)."""

import logging

from app.payroll.services import jobs


def test_jc_as_15_listed_modules_are_imported(monkeypatch):
    imported = []
    monkeypatch.setattr(jobs, "HANDLER_MODULES", ("json",))
    real = jobs.importlib.import_module
    monkeypatch.setattr(jobs.importlib, "import_module", lambda name: imported.append(name) or real(name))
    jobs.load_handlers()
    assert "json" in imported
    assert all(not name.startswith("app.services.") for name in imported if name != "json")  # never a scan


def test_jc_as_15_future_module_is_skipped_quietly(monkeypatch, caplog):
    monkeypatch.setattr(jobs, "HANDLER_MODULES", ("app.services.attendance_jobs",))
    monkeypatch.setattr(jobs, "FUTURE_HANDLER_MODULES", frozenset({"app.services.attendance_jobs"}))
    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        jobs.load_handlers()
    assert "attendance_jobs" not in caplog.text


def test_jc_as_15_missing_module_that_should_exist_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(jobs, "HANDLER_MODULES", ("app.services.no_such_handlers",))
    with caplog.at_level(logging.ERROR, logger=jobs.logger.name):
        jobs.load_handlers()
    assert "Could not import job handlers from app.services.no_such_handlers" in caplog.text
