"""pytest-parallex plugin entry point."""

from __future__ import annotations

import os
from typing import Any

import pytest


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


@pytest.fixture(scope="session")
def worker_id(request: pytest.FixtureRequest) -> str:
    """Return 'w0', 'w1', ... or 'main' if not in parallel mode."""
    return getattr(request.config, "_parallel_worker_id", "main")


@pytest.fixture(scope="session")
def parallel_setup_data(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Data returned by the ``pytest_parallex_setup`` hook."""
    return getattr(request.config, "_parallel_setup_data", {})


@pytest.fixture(scope="session")
def parallel_mode(request: pytest.FixtureRequest) -> str | None:
    """The active parallel mode, or None."""
    return request.config.getoption("parallel_mode", default=None)
