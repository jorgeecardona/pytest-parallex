"""Thread and fork safety utilities for parallel test execution."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

import pytest


# --- Fork Safety ---


class ForkUnsafe(Exception):
    """The process cannot be safely forked. The message names the offender."""


def assert_forkable() -> None:
    """Raise ``ForkUnsafe`` unless this process is quiet enough to ``fork()``.

    ``fork()`` duplicates only the calling thread. Anything the other threads held —
    a lock, a half-written buffer, a connection's state machine — is duplicated
    mid-flight and belongs to no one in the child, which is how forked children
    deadlock. CPython 3.12 warns about exactly this (DeprecationWarning: "This process
    is multi-threaded, use of fork() may lead to deadlocks in the child").

    So we refuse rather than warn, and name what to move after the fork.
    """
    extra = [t for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
    if extra:
        names = ", ".join(sorted(t.name for t in extra[:4]))
        raise ForkUnsafe(f"{len(extra)} non-main thread(s) are running ({names})")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop running — the state we want
    else:
        raise ForkUnsafe("an asyncio event loop is already running")

    if sys.platform == "win32" or not hasattr(os, "fork"):
        raise ForkUnsafe(f"os.fork() is not available on {sys.platform}")


# --- Thread-Local SetupState Proxy ---


class ThreadLocalSetupStateProxy:
    """Proxy that gives each thread its own SetupState instance.

    Pytest's ``session._setupstate`` tracks fixture setup/teardown using a
    stack. Concurrent modifications from multiple threads cause assertion
    errors. This proxy ensures each thread operates on its own stack.
    """

    def __init__(self) -> None:
        self._local = threading.local()
        self._all_states: list[Any] = []
        self._lock = threading.Lock()

    def _get_state(self) -> Any:
        state = getattr(self._local, "state", None)
        if state is None:
            from _pytest.runner import SetupState

            state = SetupState()
            self._local.state = state
            with self._lock:
                self._all_states.append(state)
        return state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_state(), name)

    def teardown_all(self) -> None:
        """Tear down all thread-local states."""
        for state in self._all_states:
            state.teardown_exact(None)


# --- Fixture Locking ---

_fixture_locks: dict[int, threading.Lock] = {}
_fixture_locks_lock = threading.Lock()


def _get_fixture_lock(fixturedef_id: int) -> threading.Lock:
    with _fixture_locks_lock:
        if fixturedef_id not in _fixture_locks:
            _fixture_locks[fixturedef_id] = threading.Lock()
        return _fixture_locks[fixturedef_id]


class ThreadFixtureLocking:
    """Prevents concurrent setup of non-function-scoped fixtures.

    When multiple threads need the same module/session-scoped fixture,
    only the first thread runs setup. Others wait and get the cached result.
    """

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest  # type: ignore[type-arg]
    ) -> Any:
        if fixturedef.scope == "function":
            return (yield)

        lock = _get_fixture_lock(id(fixturedef))
        with lock:
            if fixturedef.cached_result is not None:
                return fixturedef.cached_result[0]
            return (yield)


# --- Thread-Safe Reporter ---


class ThreadSafeReporter:
    """Serializes terminal output from concurrent test reporters."""

    _lock = threading.Lock()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> pytest.TestReport:  # type: ignore[return]
        with self._lock:
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logstart(self) -> None:
        with self._lock:
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logfinish(self) -> None:
        with self._lock:
            return (yield)


def clear_fixture_locks() -> None:
    """Clear all fixture locks. Call at session end."""
    with _fixture_locks_lock:
        _fixture_locks.clear()
