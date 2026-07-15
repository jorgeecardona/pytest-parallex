"""Thread-based parallel test runner.

Runs tests on plain threads that share one pytest session, so ``scope="session"``
fixtures are built exactly once and every worker uses the same object — no fork, no
serialization, and it works on Windows.

What it cannot escape is the GIL: a suite that is genuinely CPU-bound in Python will not
get faster here. This mode is for suites that spend their time waiting — on a network, a
database, a subprocess — which is most test suites.

The cost is that tests must tolerate sharing an address space with each other: a test
that chdir's, monkeypatches a global, or mutates a module-level cache will be seen doing
it by every other test running at that moment. When that bites, use ``--parallel=fork``
(shared setup, isolated execution) or ``--parallel=process``.
"""

from __future__ import annotations

import threading

import pytest

from pytest_parallex._loop import raise_if_collection_failed, run_chunk
from pytest_parallex._safety import (
    ThreadLocalSetupStateProxy,
    ThreadSafeReporter,
    clear_fixture_locks,
    install_fixture_execute_lock,
)
from pytest_parallex.scheduler import schedule_round_robin


class ThreadRunner:
    def __init__(self, config: pytest.Config, num_workers: int) -> None:
        self.config = config
        self.num_workers = num_workers

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        data = self.config.hook.pytest_parallex_setup(config=self.config)
        self.config._parallel_setup_data = data or {}  # type: ignore[attr-defined]
        self.config.pluginmanager.register(ThreadSafeReporter(), "thread-safe-reporter")
        install_fixture_execute_lock()

    @pytest.hookimpl
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        raise_if_collection_failed(session)

        if not session.items:
            return True

        # Replace SetupState with thread-local proxy
        proxy = ThreadLocalSetupStateProxy()
        session._setupstate = proxy  # type: ignore[assignment]

        chunks = schedule_round_robin(session.items, self.num_workers)

        errors: list[BaseException] = []
        threads: list[threading.Thread] = []

        for i, chunk in enumerate(chunks):
            t = threading.Thread(
                target=run_chunk,
                args=(chunk, session, errors, f"w{i}"),
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        proxy.teardown_all()

        if errors:
            raise errors[0]

        return True

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        clear_fixture_locks()
        data = getattr(self.config, "_parallel_setup_data", {})
        self.config.hook.pytest_parallex_teardown(config=self.config, data=data)
