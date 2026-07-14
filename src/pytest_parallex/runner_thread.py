"""Thread-based parallel test runner.

Uses ThreadPoolExecutor to run tests in threads sharing a single pytest
session. Session-scoped fixtures run exactly once.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from pytest_parallex import _worker_id_local
from pytest_parallex._safety import (
    ThreadFixtureLocking,
    ThreadLocalSetupStateProxy,
    ThreadSafeReporter,
    clear_fixture_locks,
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
        self.config.pluginmanager.register(ThreadFixtureLocking(), "thread-fixture-locking")

    @pytest.hookimpl
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(
                "%d error%s during collection"
                % (session.testsfailed, "s" if session.testsfailed != 1 else "")
            )

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
                target=self._run_chunk,
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

    @staticmethod
    def _run_chunk(
        items: list[Any],
        session: pytest.Session,
        errors: list[BaseException],
        worker_id: str,
    ) -> None:
        _worker_id_local.id = worker_id
        try:
            for i, item in enumerate(items):
                # Use session as nextitem for last item to prevent premature
                # session-scoped teardown while other threads are still running
                nextitem = items[i + 1] if i + 1 < len(items) else session
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
                if session.shouldfail or session.shouldstop:
                    break
        except BaseException as exc:
            errors.append(exc)
