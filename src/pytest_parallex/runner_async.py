"""Async parallel test runner — the thread runner, driven from an event loop.

``asyncio.to_thread`` dispatches to a real thread pool, so this is ``--parallel=thread``
with an asyncio front end: same shared session, same fixture caching, same GIL, same
requirement that tests tolerate sharing an address space. Everything of substance lives in
``_safety`` and ``_loop.run_chunk``, shared with the thread runner.

Reach for it when the surrounding code is already async and driving the run from a loop is
convenient. If you just want tests to run in parallel, ``--parallel=thread`` is the same
thing with one less moving part.
"""

from __future__ import annotations

import asyncio

import pytest

from pytest_parallex._loop import raise_if_collection_failed, run_chunk
from pytest_parallex._safety import (
    ThreadLocalSetupStateProxy,
    ThreadSafeReporter,
    clear_fixture_locks,
    install_fixture_thread_safety,
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
        install_fixture_thread_safety()

    @pytest.hookimpl
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        raise_if_collection_failed(session)

        if not session.items:
            return True

        # Replace SetupState with thread-local proxy
        proxy = ThreadLocalSetupStateProxy()
        session._setupstate = proxy  # type: ignore[assignment]

        asyncio.run(self._run_all(session))

        proxy.teardown_all()
        return True

    async def _run_all(self, session: pytest.Session) -> None:
        """One task per *worker*, each owning a chunk — not one task per test.

        Per-test tasks were the original design and they were unsound. ``to_thread``
        hands work to a shared pool, so any thread could run any item, and each item's
        ``nextitem`` pointed at the globally-next item rather than the next one on its own
        worker. Two threads then interleaved teardown of scopes the other was still
        inside, and pytest threw ``AssertionError: previous item was not torn down
        properly`` from a thread that had done nothing wrong. Chunking is what makes
        ``nextitem`` meaningful — see ``_loop.run_chunk``.

        No semaphore either: the chunk count *is* the worker count, so concurrency is
        bounded by construction.
        """
        chunks = schedule_round_robin(session.items, self.num_workers)
        errors: list[BaseException] = []

        await asyncio.gather(
            *(
                asyncio.to_thread(run_chunk, chunk, session, errors, f"a{i}")
                for i, chunk in enumerate(chunks)
            )
        )

        if errors:
            raise errors[0]

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        clear_fixture_locks()
        data = getattr(self.config, "_parallel_setup_data", {})
        self.config.hook.pytest_parallex_teardown(config=self.config, data=data)
