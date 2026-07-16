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
    # forward --parallel, so `mode` is None for a worker.
    report_path = os.environ.get(ENV_REPORT_PATH)
    if report_path:
        config.pluginmanager.register(ReportWriter(report_path), "parallex-report-writer")
        config._parallel_worker_id = os.environ.get(  # type: ignore[attr-defined]
            "PYTEST_PARALLEL_WORKER", "main"
        )

    mode = config.getoption("parallel_mode", default=None)

    # Registered for the controller *and* for a spawned worker: `mode` is None in the
    # worker, and returning before this left it without the fixtures — a test asking for
    # worker_id then failed with "fixture not found", but only under --parallel=process,
    # and only in the worker, where the controller replays the error as a bare setup
    # failure.
    if mode is not None or report_path:
        # Late, so our worker_id beats xdist's — see WorkerIdFixtures. Never for a plain
        # xdist run, which must keep xdist's.
        config.pluginmanager.register(WorkerIdFixtures(), "parallex-worker-id")
    elif not config.pluginmanager.hasplugin("xdist"):
        # Serial run, xdist not installed: provide worker_id anyway, returning 'main'.
        # The archetype `def database_url(worker_id, server)` only works if it resolves in
        # EVERY way the suite is run — a conftest can't request a fixture that exists only
        # under --parallel, so without this, adopting the pattern broke plain `pytest`
        # (found immediately on the first real adopter: accounting has no xdist, and its
        # serial run died with "fixture 'worker_id' not found"). When xdist IS installed
        # and idle we stay out of the way: its own fixture answers 'master', and suites
        # written against it may compare on exactly that string.
        config.pluginmanager.register(WorkerIdFixtures(), "parallex-worker-id")

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
    # `raw is None` shouldn't happen (the option defaults to "auto"), but getoption is
    # typed Any | None and None is exactly the value int() cannot take — so treat it as
    # the default rather than let it reach int().
    if raw is not None and raw != "auto":
        try:
            n = int(raw)
        except (TypeError, ValueError):
            # A UsageError, not the ValueError this used to raise. Anything raised from
            # pytest_configure that isn't a UsageError comes out as an INTERNALERROR and
            # a traceback through pytest's guts — for a typo in a flag.
            raise pytest.UsageError(
                f"--parallel-workers must be an integer or 'auto', not {raw!r}"
            ) from None
        if n < 1:
            raise pytest.UsageError(f"--parallel-workers must be >= 1, not {n}")
        return n

    result = config.hook.pytest_parallex_auto_num_workers(config=config)
    if result is not None:
        return result

    return os.cpu_count() or 1


# --- Fixtures ---


def _current_worker_id(config: pytest.Config) -> str:
    """Who is running this test: 'f0'/'w0'/'a0'…, or 'main' if not parallel.

    The two runner families report their identity differently — fork sets a config
    attribute in the child (its own process, so there is nothing to isolate it from),
    thread/async set a thread-local (one process, so there is). Ask the thread-local
    first: in fork children it is unset and falls through, and in thread/async the config
    attribute is unset, so exactly one of them ever answers.
    """
    worker = get_worker_id()
    if worker != "main":
        return worker
    return getattr(config, "_parallel_worker_id", "main")


class WorkerIdFixtures:
    """Provides ``worker_id``, registered late enough to beat pytest-xdist to the name.

    ``worker_id`` is xdist's name, and taking it is deliberate rather than careless.
    ``session_scope.is_worker_scoped`` uses a dependency on it as the marker for "this
    fixture is per-worker, do not hoist it into the controller" — and that only works
    because suites *already write it that way*: ``def database_url(worker_id): ...`` is
    what an xdist suite looks like today. A name of our own would mark nothing that
    already exists.

    So it must be *our* value, and there the plugin was losing on a technicality.
    Fixtures from two plugins resolve by registration order, pytest11 entry points load
    alphabetically, and 'parallex' sorts before 'xdist' — so xdist's ``worker_id``
    registered second and won. Every test silently got xdist's ``'master'``.

    That is not cosmetic. ``worker_id`` marks a fixture as per-worker, so
    ``database_url(worker_id)`` is correctly left un-hoisted — and then all four workers
    resolve ``worker_id`` to the same ``'master'`` and carve the same database, wiping
    each other's schemas. Right shape, wrong answer, no error: the exact failure the
    marker exists to prevent.

    Registering here — from ``pytest_configure``, after every plugin has loaded — puts
    us last and wins the name. Only when ``--parallel`` is active: an actual xdist run
    must keep xdist's.

    Both are ``scope="session"``, matching xdist. That is not a copied detail, it is what
    makes the archetype legal at all: ``database_url`` is session-scoped, and a
    session-scoped fixture may only request session-scoped fixtures. A function-scoped
    ``worker_id`` turns ``def database_url(worker_id)`` — the exact line this is meant to
    support — into a ``ScopeMismatch`` error.

    Session scope is also the *right* answer wherever a worker is a process: under fork
    and process each child resolves it once, in itself, and gets its own id. (It is never
    hoisted to the controller, because requesting it is precisely what marks a fixture
    per-worker.) Under thread and async, where all workers share one session, one cached
    value is all a session-scoped fixture can be — so a per-worker session fixture is not
    expressible there. That is a property of sharing an interpreter, not of this code.
    """

    @pytest.fixture(scope="session")
    def worker_id(self, request: pytest.FixtureRequest) -> str:
        """The parallex worker: 'f0', 'w1', 'a2'… or 'main' when not parallel.

        A fixture that requests this is per-worker by definition and is never set up in
        the controller — see ``session_scope.is_worker_scoped``.
        """
        return _current_worker_id(request.config)

    @pytest.fixture(scope="session")
    def parallex_worker_id(self, request: pytest.FixtureRequest) -> str:
        """``worker_id`` under a name that cannot collide with xdist's. Same value."""
        return _current_worker_id(request.config)


@pytest.fixture(scope="session")
def parallex_setup_data(request: pytest.FixtureRequest) -> dict[str, Any]:
    """Data returned by the ``pytest_parallex_setup`` hook."""
    return getattr(request.config, "_parallel_setup_data", {})


@pytest.fixture(scope="session")
def parallex_mode(request: pytest.FixtureRequest) -> str | None:
    """The active parallel mode, or None."""
    return request.config.getoption("parallel_mode", default=None)
