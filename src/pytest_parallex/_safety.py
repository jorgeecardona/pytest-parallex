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

    _warn_darwin_fork_caveat()


def harden_darwin_fork_child() -> None:
    """Disarm macOS's ``_scproxy`` segfault inside a forked worker. No-op elsewhere.

    Verified root cause from a real macOS suite: forked workers died with
    SIGSEGV — wait status 11, zero results — inside CPython's
    ``urllib.request.proxy_bypass_macosx_sysconf``, reached from botocore's proxy
    detection during client creation. That path calls the SystemConfiguration framework
    via ``_scproxy``, which is documented fork-unsafe on macOS (CPython #105912,
    bpo-31818). ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY`` does NOT cover it (tested; no
    effect) — that variable disarms the ObjC ``+initialize`` check, a different trap.

    The patch point matters, and the first attempt got it wrong. Rebinding the *callers*
    (``urllib.request.proxy_bypass``/``getproxies``) misses anyone who captured them by
    value before the fork — and botocore does exactly that, ``from urllib.request import
    getproxies, proxy_bypass`` at import time, in the controller. An env-var fallback
    (``no_proxy='*'``) misses tests that wipe the environment — ``patch.dict(os.environ,
    ..., clear=True)`` re-armed the crash mid-suite. So instead this patches the
    *leaves*: the original ``proxy_bypass`` resolves ``proxy_bypass_macosx_sysconf`` in
    ``urllib.request``'s module globals **at call time**, so replacing the leaf catches
    every caller, however early they imported, whatever the environment says. Verified on
    the same suite: 0 dead workers, all 530 claims accounted for, and the fastest
    configuration measured on that machine (30.0s vs xdist's 38.2s).

    The stubs say "no proxies", which for a forked test worker is the only sane answer.
    A worker that must exercise real macOS proxy discovery cannot run under fork() at
    all; use ``--parallel=process`` for that suite.
    """
    if sys.platform != "darwin":
        return

    import urllib.request

    def _no_proxy_bypass(host: object, *args: object, **kwargs: object) -> bool:
        del host, args, kwargs
        return False

    def _no_proxies() -> dict[str, str]:
        return {}

    urllib.request.proxy_bypass_macosx_sysconf = _no_proxy_bypass  # type: ignore[attr-defined]
    urllib.request.getproxies_macosx_sysconf = _no_proxies  # type: ignore[attr-defined]


def _warn_darwin_fork_caveat() -> None:
    """On macOS, warn up front about the Objective-C fork trap.

    Since High Sierra, the ObjC runtime deliberately crashes a forked child the moment it
    touches an Apple framework whose ``+initialize`` had run in the parent —
    ``objc[pid]: +[...] may have been in progress in another thread when fork() was
    called ... Crashing instead.`` To the user that is a wall of "Python quit
    unexpectedly" dialogs, one per worker, with the real message only on stderr. It is
    armed by ordinary imports: urllib's proxy lookup (SystemConfiguration), ssl/keychain,
    anything CoreFoundation. This is why multiprocessing defaults to spawn on macOS since
    Python 3.8.

    Apple's own escape hatch is ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` — but the ObjC
    runtime reads it at process start, so setting it here, mid-process, would do nothing.
    All we can do is say so before the crashes instead of after.
    """
    if sys.platform != "darwin" or os.environ.get("OBJC_DISABLE_INITIALIZE_FORK_SAFETY"):
        return
    import warnings

    warnings.warn(
        "--parallel=fork on macOS: forked workers can be killed by the Objective-C "
        "runtime if any Apple framework was initialized before the fork (seen as "
        "'Python quit unexpectedly' dialogs). The fork-unsafe _scproxy/proxy-lookup "
        "path is disarmed automatically in workers, but the ObjC +initialize trap "
        "is not disarmable from inside a running process: if workers crash, export "
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES before running pytest, or use "
        "--parallel=process (the dependable mode on macOS).",
        stacklevel=2,
    )


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
_MISSING = object()


def _get_fixture_lock(fixturedef_id: int) -> threading.RLock:
    with _fixture_locks_lock:
        if fixturedef_id not in _fixture_locks:
            # Reentrant: execute() resolves dependencies inside the lock, and a diamond
            # (X needs Y, Z needs Y) can re-enter the same fixturedef on one thread.
            _fixture_locks[fixturedef_id] = threading.RLock()
        return _fixture_locks[fixturedef_id]


class _ScopeAwareState:
    """A ``FixtureDef`` attribute that is per-thread for function scope, shared otherwise.

    This is the fix for the bug that hid behind "function fixtures have nothing to share".
    The *value* a function fixture produces is per-test, yes — but the ``FixtureDef``
    object holding it is one object shared by every worker, and pytest keeps a *single*
    ``cached_result`` (and one ``_finalizers`` list) on it, tearing the previous test's
    value down before building the next. Two threads running two tests then trample one
    slot: thread B's setup calls ``finish()`` on thread A's still-live fixture, and pytest
    dies with a bare ``AssertionError`` from inside ``execute``, or a fixture silently
    fails to set up at all. A plain 2-test/2-worker run hit it about half the time; it was
    shipped broken.

    Serializing ``execute`` cannot fix this — the collision is between one thread's setup
    and another's *teardown*, which runs on a different call path. The slot itself has to
    be per-thread.

    Installed as a data descriptor on the ``FixtureDef`` class, so it intercepts every
    instance including ones collected before we patched. Function scope reads and writes a
    ``threading.local`` kept on the instance; every other scope reads and writes the
    instance dict exactly as before, because session/module/class fixtures are *meant* to
    be one shared value — that is the whole plugin.
    """

    def __init__(self, name: str, default_factory: Any) -> None:
        self._shared = f"_parallex_shared_{name}"
        self._tls = f"_parallex_tls_{name}"
        self._default_factory = default_factory  # e.g. list, or a lambda returning None

    def _thread_slot(self, obj: Any) -> Any:
        tls = obj.__dict__.get(self._tls)
        if tls is None:
            tls = threading.local()
            obj.__dict__[self._tls] = tls
        return tls

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self
        if obj.scope == "function":
            tls = self._thread_slot(obj)
            value = getattr(tls, "value", _MISSING)
            if value is _MISSING:
                value = self._default_factory()
                tls.value = value
            return value
        value = obj.__dict__.get(self._shared, _MISSING)
        if value is _MISSING:
            value = self._default_factory()
            obj.__dict__[self._shared] = value
        return value

    def __set__(self, obj: Any, value: Any) -> None:
        if obj.scope == "function":
            self._thread_slot(obj).value = value
        else:
            obj.__dict__[self._shared] = value


def install_fixture_thread_safety() -> None:
    """Make ``FixtureDef`` safe to drive from several threads, for the threaded runners.

    Two patches on one pytest internal, installed together because they are one concern:

    * ``execute`` is serialized per fixturedef so a *shared* (session/module/class) fixture
      is built exactly once even when several workers request it at once. The critical
      section starts before any hook we could register runs, so wrapping the method is the
      shallowest thing that works. Function scope skips the lock — it now has its own
      per-thread state and nothing to contend on.
    * ``cached_result`` and ``_finalizers`` become per-thread for function scope (see
      ``_ScopeAwareState``), so two workers running two tests don't share one setup slot.

    Deadlock on the lock: a fixture resolves its dependencies while holding its own lock,
    so A-holds-X-wants-Y against B-holds-Y-wants-X needs X and Y to depend on each other —
    a cycle, which pytest rejects at collection.
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
    FixtureDef.cached_result = _ScopeAwareState("cached_result", lambda: None)  # type: ignore[assignment]
    FixtureDef._finalizers = _ScopeAwareState("_finalizers", list)  # type: ignore[assignment]

    _install_tolerant_current_test_var()


_original_current_test_var: Any = None


def _install_tolerant_current_test_var() -> None:
    """Stop pytest's ``PYTEST_CURRENT_TEST`` bookkeeping from crashing under threads.

    pytest keeps the name of the running test in the ``PYTEST_CURRENT_TEST`` environment
    variable and deletes it at teardown with a bare ``os.environ.pop(name)`` — no default,
    so a missing key raises. ``os.environ`` is one process-global dict shared by every
    worker thread, and several tests are "current" at once, so two teardowns racing to pop
    it means the second gets ``KeyError: 'PYTEST_CURRENT_TEST'`` and the test errors in
    teardown having actually passed.

    The value is only ever used to name the running test in a crash dump, and it is
    already meaningless here (whichever thread wrote last wins). So we make the delete
    tolerant and leave the rest alone. xdist never hits this because each worker is its own
    process with its own environ; sharing one is the cost of thread-based parallelism.
    """
    global _original_current_test_var
    if _original_current_test_var is not None:
        return

    from _pytest import runner

    _original_current_test_var = runner._update_current_test_var

    def tolerant(item, when):  # type: ignore[no-untyped-def]
        if when is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)  # was pop(name) — raised on a race
        else:
            _original_current_test_var(item, when)

    runner._update_current_test_var = tolerant  # type: ignore[attr-defined]


def _uninstall_tolerant_current_test_var() -> None:
    global _original_current_test_var
    if _original_current_test_var is None:
        return
    from _pytest import runner

    runner._update_current_test_var = _original_current_test_var  # type: ignore[attr-defined]
    _original_current_test_var = None


def uninstall_fixture_thread_safety() -> None:
    """Put ``FixtureDef`` back.

    Not hygiene for its own sake: pytest's own ``pytester`` runs test sessions in *this*
    interpreter, so a patch left installed leaks out of the session that made it and into
    every later one — including the suite that is testing this plugin.
    """
    global _original_execute
    if _original_execute is None:
        return

    from _pytest.fixtures import FixtureDef

    FixtureDef.execute = _original_execute  # type: ignore[method-assign]
    # Drop the descriptors so the next in-process session's FixtureDefs use plain instance
    # attributes again. Their leftover ``_parallex_*`` dict keys are harmless — nothing
    # reads them once the descriptor is gone.
    for attr in ("cached_result", "_finalizers"):
        if isinstance(FixtureDef.__dict__.get(attr), _ScopeAwareState):
            delattr(FixtureDef, attr)
    _uninstall_tolerant_current_test_var()
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
    uninstall_fixture_thread_safety()
    with _fixture_locks_lock:
        _fixture_locks.clear()
