"""Async I/O based parallel test runner.

Uses asyncio to run tests concurrently via asyncio.to_thread.
Shares thread safety utilities with the thread runner since
to_thread dispatches to a real thread pool.
"""

from __future__ import annotations

import asyncio
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


class AsyncRunner:
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

        asyncio.run(self._run_all(session))

        proxy.teardown_all()
        return True

    async def _run_all(self, session: pytest.Session) -> None:
        semaphore = asyncio.Semaphore(self.num_workers)
        errors: list[BaseException] = []

        async def run_one(
            item: pytest.Item, nextitem: pytest.Item | pytest.Session
        ) -> None:
            async with semaphore:
                if session.shouldfail or session.shouldstop:
                    return
                try:
                    await asyncio.to_thread(
                        self._run_item, item, nextitem, session
                    )
                except BaseException as exc:
                    errors.append(exc)

        tasks = []
        items = session.items
        for i, item in enumerate(items):
            nextitem = items[i + 1] if i + 1 < len(items) else session
            tasks.append(asyncio.create_task(run_one(item, nextitem)))

        await asyncio.gather(*tasks, return_exceptions=True)

        if errors:
            raise errors[0]

    @staticmethod
    def _run_item(
        item: pytest.Item,
        nextitem: Any,
        session: pytest.Session,
    ) -> None:
        _worker_id_local.id = f"async-{id(asyncio.current_task())}"
        item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        clear_fixture_locks()
        data = getattr(self.config, "_parallel_setup_data", {})
        self.config.hook.pytest_parallex_teardown(config=self.config, data=data)
