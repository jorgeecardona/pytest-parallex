"""Thread and fork safety utilities for parallel test execution."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import threading
from collections.abc import Generator
from typing import Any

import pytest

# --- Fork Safety ---


class ForkUnsafe(Exception):
    """The process cannot be safely forked. The message names the offender."""


def quiesce_known_threads() -> list[object]:
    """Stop background threads we know how to restart. Returns them, for restarting.

    ``fork()`` needs a single-threaded process, but a test suite is single-threaded only
    until some import starts a helper thread — and refusing to fork because of one is
    useless in practice. The common offender by far is the stdlib's logging
    ``QueueListener``: any app configuring a ``QueueHandler`` gets a ``_monitor`` thread at
    import, litestar included (``litestar.logging.standard.LoggingQueueListener`` starts one
    in its ``__init__``).

    A ``QueueListener`` is safe to bounce because it says so itself: ``stop()`` drains the
    queue and joins the thread, ``start()`` spins a fresh one. So we stop them before the
    fork and restart them after, in the parent and in every child.

    We only touch threads we can identify AND restart. Anything else still fails
    ``assert_forkable`` — guessing at a thread we don't understand is how children deadlock.
    """
    stopped: list[object] = []
    for thread in threading.enumerate():
        if thread is threading.main_thread() or not thread.is_alive():
            continue
        target = getattr(thread, "_target", None)
        owner = getattr(target, "__self__", None)
        if isinstance(owner, logging.handlers.QueueListener):
            owner.stop()
            stopped.append(owner)
    return stopped


def restart_quiesced(listeners: list[object]) -> None:
    """Restart what ``quiesce_known_threads`` stopped. Safe in parent and child alike."""
    for listener in listeners:
        listener.start()  # type: ignore[attr-defined]


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
#
# Making a shared, non-function-scoped fixture safe under threads means serializing
# ``FixtureDef.execute``, and nothing shallower will do. The sequence execute() runs is:
#
#     1. resolve the fixture's own dependencies
#     2. check cached_result -> return early on a hit
#     3. assert not self._finalizers, register finalizers      (pytest >= 9.1)
#     4. call the pytest_fixture_setup hook -> run the body, populate the cache
#
# Every plugin hook available to us fires at step 4. The race is between 2 and 3: two
# threads both miss the cache, the first reaches 4 and registers its finalizers, and the
# second — already past its own step 2 — hits the assertion at 3 and dies with a bare
# ``AssertionError`` from deep inside pytest. Locking at step 4 is locking after the
# accident.
#
# pytest 9.0 had no such assertion, so the same race merely ran the fixture body twice and
# said nothing: one container booted per thread, silently, which is the exact behaviour
# this plugin exists to prevent. 9.1 turned it into a crash. Both are the same bug.
#
# So we wrap execute() itself, which puts steps 1-4 in one critical section. The waiter
# then finds the cache at its *own* step 2 and returns early through pytest's own code
# path — no short-circuit of ours, no second body, no assertion.

_fixture_locks: dict[int, threading.RLock] = {}
_fixture_locks_lock = threading.Lock()
_original_execute: Any = None


def _get_fixture_lock(fixturedef_id: int) -> threading.RLock:
    with _fixture_locks_lock:
        if fixturedef_id not in _fixture_locks:
            # Reentrant: execute() resolves dependencies inside the lock, and a diamond
            # (X needs Y, Z needs Y) can re-enter the same fixturedef on one thread.
            _fixture_locks[fixturedef_id] = threading.RLock()
        return _fixture_locks[fixturedef_id]


def install_fixture_execute_lock() -> None:
    """Serialize ``FixtureDef.execute`` per fixturedef, for the threaded runners.

    Patching a pytest internal is a real cost, taken because the alternative doesn't
    exist: the critical section starts before any hook we can register runs.

    Function-scoped fixtures are left alone. They are per-test by definition, so there is
    nothing to share and no reason to make every test queue behind one lock.

    Deadlock: a fixture resolves its dependencies while holding its own lock, so thread A
    can hold X and want Y while B holds Y and wants X only if X and Y depend on each
    other — a cycle, which pytest rejects at collection.
    """
    global _original_execute
    if _original_execute is not None:
        return  # already installed

    from _pytest.fixtures import FixtureDef

    _original_execute = FixtureDef.execute

    def locked_execute(self, request):  # type: ignore[no-untyped-def]
        if self.scope == "function":
            return _original_execute(self, request)
        with _get_fixture_lock(id(self)):
            return _original_execute(self, request)

    FixtureDef.execute = locked_execute  # type: ignore[method-assign]


def uninstall_fixture_execute_lock() -> None:
    """Put ``FixtureDef.execute`` back.

    Not hygiene for its own sake: pytest's own ``pytester`` runs test sessions in *this*
    interpreter, so a patch left installed leaks out of the session that made it and into
    every later one — including the suite that is testing this plugin.
    """
    global _original_execute
    if _original_execute is None:
        return

    from _pytest.fixtures import FixtureDef

    FixtureDef.execute = _original_execute  # type: ignore[method-assign]
    _original_execute = None


# --- Thread-Safe Reporter ---


class ThreadSafeReporter:
    """Serializes terminal output from concurrent test reporters.

    Annotated ``Generator[None, Any, Any]``, not with the hook's own return type: a
    pluggy ``wrapper=True`` hookimpl *is* a generator, and ``return (yield)`` returns
    whatever the inner implementations produced. The previous annotations described the
    hook rather than the function, so pyright rejected all three — and the
    ``# type: ignore[return]`` meant to paper over it sat on the ``def`` line while the
    error is reported on the ``yield``.
    """

    _lock = threading.Lock()

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> Generator[None, Any, Any]:
        with self._lock:
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logstart(self) -> Generator[None, Any, Any]:
        with self._lock:
            return (yield)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_logfinish(self) -> Generator[None, Any, Any]:
        with self._lock:
            return (yield)


def clear_fixture_locks() -> None:
    """Drop the per-fixturedef locks and restore ``execute``. Call at session end.

    The locks are keyed by ``id(fixturedef)``, which CPython reuses once the object is
    collected — so a lock kept past the session it belongs to could later be handed out
    for an unrelated fixture that happens to land on the same address.
    """
    uninstall_fixture_execute_lock()
    with _fixture_locks_lock:
        _fixture_locks.clear()
