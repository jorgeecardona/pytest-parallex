"""Ferry test reports from a worker back to the controller.

A worker that runs tests in its own process — forked or spawned — writes its results to
its own stdout, where the controller cannot see them, count them, or fold them into one
summary. So workers don't print: they capture each report, serialize it, and the
controller replays the lot on its own hooks, where the terminal reporter, ``-p junitxml``,
and every other reporting plugin see them exactly as if the test had run locally.

``TestReport._to_json``/``_from_json`` are the transport. They are private, but they are
the same pair xdist has used for this for years, and they exist precisely because a
``TestReport`` holds live objects (``ExceptionInfo``, traceback frames) that pickle cannot
carry across a process boundary.
"""

from __future__ import annotations

import contextlib
import os
import pickle
from typing import Any

import pytest

#: Set on a spawned worker's environment to name the file its reports land in. The plugin
#: keys off its presence to decide it is a worker rather than a controller.
ENV_REPORT_PATH = "PARALLEX_REPORT_PATH"


class ReportCollector:
    """Captures reports in memory instead of letting them reach the terminal."""

    def __init__(self) -> None:
        self.collected: list[Any] = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.collected.append(report._to_json())


class ReportWriter(ReportCollector):
    """A collector that flushes to ``path`` when the worker's session ends.

    ``trylast`` so it runs after the reports themselves have all been logged.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self) -> None:
        write_reports(self.path, self.collected)


def write_reports(path: str, collected: list[Any]) -> None:
    with open(path, "wb") as fh:
        pickle.dump(collected, fh, protocol=pickle.HIGHEST_PROTOCOL)


def read_reports(path: str) -> list[Any]:
    """Read a worker's reports. A worker that died before writing yields no reports."""
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except (OSError, EOFError, pickle.UnpicklingError):
        return []


def replay(config: pytest.Config, payload: list[Any]) -> None:
    """Replay a worker's reports on the controller's hooks.

    Note what this deliberately does *not* do: count failures. ``Session`` implements
    ``pytest_runtest_logreport`` itself, so dispatching here already bumps
    ``session.testsfailed`` — and does it better than a caller could, because it also
    honours ``wasxfail`` (an xfail arrives as ``report.failed``, and naive counting turns
    every expected failure into a real one) and trips ``maxfail``. A caller that counts
    ``report.failed`` on top of this reports every failure twice, and counts setup, call
    and teardown as three.
    """
    from _pytest.reports import TestReport

    for raw in payload:
        config.hook.pytest_runtest_logreport(report=TestReport._from_json(raw))


def unlink_quietly(path: str) -> None:
    """Remove a worker's report file. A failure here must not fail the run."""
    with contextlib.suppress(OSError):
        os.unlink(path)
