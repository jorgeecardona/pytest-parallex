"""pytest-parallex plugin entry point."""

from __future__ import annotations

import os
from typing import Any

import pytest

from pytest_parallex import get_worker_id
from pytest_parallex._reports import ENV_REPORT_PATH, ReportWriter


def pytest_addhooks(pluginmanager: pytest.PytestPluginManager) -> None:
    from pytest_parallex import hooks

    pluginmanager.add_hookspecs(hooks)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("parallel", "parallel test execution")
    group.addoption(
        "--parallel",
        dest="parallel_mode",
        metavar="MODE",
        choices=["thread", "process", "async", "fork"],
        default=None,
        help="Run tests in parallel. MODE: thread, process, async, or fork.",
    )
    group.addoption(
        "--parallel-workers",
        dest="parallel_workers",
        metavar="N",
        default="auto",
        help="Number of parallel workers (default: auto = CPU count).",
    )
    group.addoption(
        "--parallex-no-session-scope",
        dest="parallex_session_scope",
        action="store_false",
        default=True,
        help=(
            "Don't set session-scoped fixtures up in the controller before forking. "
            "Restores xdist's behaviour, where scope='session' runs once per worker. "
            "Use when a session fixture starts a thread or opens a connection and so "
            "cannot survive the fork."
        ),
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    if config.option.collectonly:
        return

    # A spawned worker (--parallel=process) is an ordinary pytest run that happens to have
    # been handed a report path. It captures its reports for the controller to replay
    # instead of printing them, and it never fans out itself — the controller does not
    # forward --parallel, so `mode` is None here and we return below.
    report_path = os.environ.get(ENV_REPORT_PATH)
    if report_path:
        config.pluginmanager.register(ReportWriter(report_path), "parallex-report-writer")
        config._parallel_worker_id = os.environ.get(  # type: ignore[attr-defined]
            "PYTEST_PARALLEL_WORKER", "main"
        )

    mode = config.getoption("parallel_mode", default=None)
    if mode is None:
        return

    num_workers = _resolve_worker_count(config)

    match mode:
        case "thread":
            from pytest_parallex.runner_thread import ThreadRunner

            runner = ThreadRunner(config, num_workers)
        case "process":
            from pytest_parallex.runner_process import ProcessRunner

            runner = ProcessRunner(config, num_workers)
        case "async":
            from pytest_parallex.runner_async import AsyncRunner

            runner = AsyncRunner(config, num_workers)
        case "fork":
            from pytest_parallex.runner_fork import ForkRunner

            runner = ForkRunner(config, num_workers)
        case _:
            raise pytest.UsageError(f"unknown --parallel mode: {mode!r}")

    config.pluginmanager.register(runner, "parallel-runner")


def _resolve_worker_count(config: pytest.Config) -> int:
    raw = config.getoption("parallel_workers")
    if raw != "auto":
        n = int(raw)
        if n < 1:
            raise pytest.UsageError("--parallel-workers must be >= 1")
        return n

    # Try hook first
    result = config.hook.pytest_parallex_auto_num_workers(config=config)
    if result is not None:
        return result

    # Fallback to CPU count
    return os.cpu_count() or 1


# --- Fixtures ---


@pytest.fixture
def parallex_worker_id(request: pytest.FixtureRequest) -> str:
    """The worker running this test: 'f0'/'w0'/'a0'…, or 'main' if not parallel.

    Named ``parallex_``-something because pytest-xdist already ships a fixture called
    ``worker_id``, and a suite that parallelises is exactly the suite likely to have xdist
    installed alongside this. Two plugins claiming one fixture name resolve by
    registration order, and xdist won: ours silently returned xdist's ``'master'`` — the
    right shape, the wrong answer, no error anywhere. A distinct name cannot lose that
    race.

    Deliberately *not* session-scoped, in every mode:

    * thread/async share one session across workers, so a session-scoped value would be
      computed once by whichever worker asked first and then handed to all the others.
    * under fork, ``--parallex-session-scope`` sets session fixtures up in the controller
      *before* the fork — so a session-scoped ``worker_id`` would resolve to 'main' there
      and every child would inherit that answer.

    The two runner families report their identity differently — fork sets a config
    attribute in the child (its own process, so there is nothing to isolate it from),
    thread/async set a thread-local (one process, so there is). Ask the thread-local
    first: in fork children it is unset and falls through, and in thread/async the config
    attribute is unset, so exactly one of them ever answers.
    """
    worker = get_worker_id()
    if worker != "main":
        return worker
    return getattr(request.config, "_parallel_worker_id", "main")


@pytest.fixture(scope="session")
def parallex_setup_data(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Data returned by the ``pytest_parallex_setup`` hook."""
    return getattr(request.config, "_parallel_setup_data", {})


@pytest.fixture(scope="session")
def parallex_mode(request: pytest.FixtureRequest) -> str | None:
    """The active parallel mode, or None."""
    return request.config.getoption("parallel_mode", default=None)
