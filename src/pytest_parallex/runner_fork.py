"""Fork-based parallel test runner — collect once, fork warm workers.

The other runners (and pytest-xdist) start each worker as a *fresh interpreter*, so every
worker re-imports the test suite and re-collects it. On a large suite that import is the
dominant fixed cost, and it is paid ``num_workers`` times before a single test runs.

This runner pays it once. Collection happens normally in the parent, then we ``fork()``:
each child inherits the parent's already-imported modules, the collected ``session.items``,
and anything a ``pytest_parallex_warmup`` hook chose to build — all via copy-on-write, at
a cost of roughly a millisecond per child.

The trade is that ``fork()`` is only safe on a process that has no threads, no running
event loop, and no open sockets or database connections (see ``_safety.assert_forkable``).
We fork at the top of ``pytest_runtestloop``, before any fixture has run, which is the last
moment the process is still guaranteed quiet. The warmup hook runs pre-fork and is therefore
held to the same contract: pure imports and CPU work, no I/O handles.
"""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import pickle
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from pytest_parallex._safety import ForkUnsafe, assert_forkable
from pytest_parallex.scheduler import group_by_scope
from pytest_parallex.session_scope import (
    detach_in_child,
    session_fixture_names,
    setup_session_fixtures,
    teardown_session_fixtures,
)

if TYPE_CHECKING:
    from _pytest.reports import TestReport


@dataclass(frozen=True)
class _Child:
    """A forked worker and the file its serialized reports land in."""

    pid: int
    worker_id: str
    report_path: str


class ForkRunner:
    def __init__(self, config: pytest.Config, num_workers: int) -> None:
        self.config = config
        self.num_workers = num_workers

    # tryfirst is load-bearing: pytest_runtestloop is a firstresult hook, so the first
    # impl to return non-None wins and the rest never run. Registering trylast hands the
    # loop to pytest's builtin, which returns True — and this runner silently never
    # executes while every test still passes, serially. That failure is invisible except
    # as a suspiciously low core count.
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(
                "%d error%s during collection"
                % (session.testsfailed, "s" if session.testsfailed != 1 else "")
            )
        if session.config.option.collectonly or not session.items:
            return True

        # Warm up while we are still single-threaded and connection-free. Whatever this
        # builds is inherited by every child for free.
        data = self.config.hook.pytest_parallex_setup(config=self.config)
        self.config._parallel_setup_data = data or {}  # type: ignore[attr-defined]

        # Run session-scoped fixtures HERE, in the controller, so `scope="session"` means
        # once — not once per worker as it does under xdist. Children inherit the cached
        # results through the fork and never re-run them. See session_scope.py.
        self.config._parallex_session = session  # type: ignore[attr-defined]

        eager = None
        if self.config.getoption("parallex_session_scope"):
            names = session_fixture_names(session)
            if names:
                eager = setup_session_fixtures(session, names)
                if eager.names and os.environ.get("PARALLEX_TRACE"):
                    _trace(f"session fixtures set up once in controller: {', '.join(eager.names)}")
                for name, err in eager.failures:
                    _trace(f"session fixture {name!r} failed in the controller ({err}); "
                           f"leaving it to the worker that needs it")

        try:
            assert_forkable()
        except ForkUnsafe as exc:
            hint = (
                " A session fixture set up above may have started it — that setup runs "
                "pre-fork by design; use --parallex-no-session-scope to defer it to the "
                "workers."
                if eager and eager.names
                else ""
            )
            raise pytest.UsageError(
                f"--parallel=fork requires a quiet process at fork time, but {exc}.{hint} "
                f"Move the offending setup into a fixture so it runs after the fork, or "
                f"use --parallel=process."
            ) from exc

        groups = group_by_scope(session.items)
        children = self._fork_children(groups)
        failed = self._collect_reports(session, children)

        # Only the controller reaches here — children os._exit inside _fork_children — so
        # the session fixtures are finalized exactly once, after every worker is done with
        # them. Children hold inherited *copies*; letting one finalize would stop a server
        # its siblings are still using.
        if eager is not None:
            for name, err in teardown_session_fixtures(session):
                sys.stderr.write(
                    f"\npytest-parallex: session fixture {name!r} failed to tear down: {err}\n"
                )
                failed += 1

        self.config.hook.pytest_parallex_teardown(
            config=self.config, data=self.config._parallel_setup_data
        )
        if failed:
            session.testsfailed += failed
        return True

    def _fork_children(self, groups: list[list[pytest.Item]]) -> list[_Child]:
        """Fork the workers and let them race for groups off a shared counter.

        The counter lives in shared memory and is created *before* the fork, so every
        child inherits the same one and claims the next group by atomically bumping it.
        That makes this a dynamic queue rather than a static deal, which matters more
        than it sounds: a static deal has to guess a group's cost from its test count,
        and on a suite whose modules ranged 0.1s to 167s that guess left the slowest
        worker holding 239s of work against a greedy queue's 167s.
        """
        claim = multiprocessing.Value(ctypes.c_int, 0)  # index of the next unclaimed group
        children: list[_Child] = []

        for i in range(min(self.num_workers, len(groups))):
            worker_id = f"f{i}"
            fd, path = tempfile.mkstemp(prefix=f"pytest-parallex-{worker_id}-", suffix=".reports")
            os.close(fd)

            pid = os.fork()
            if pid == 0:
                # --- child ---
                code = 1
                try:
                    code = self._run_claimed(groups, claim, worker_id, path)
                finally:
                    # os._exit: skip atexit/flush handlers, which belong to the parent's
                    # copy of the world and would double-run here.
                    os._exit(code)
            children.append(_Child(pid=pid, worker_id=worker_id, report_path=path))
        return children

    def _run_claimed(
        self,
        groups: list[list[pytest.Item]],
        claim,  # multiprocessing.Value — no public type to annotate against
        worker_id: str,
        path: str,
    ) -> int:
        """Claim groups until the queue drains, then pickle this worker's reports."""
        self.config._parallel_worker_id = worker_id  # type: ignore[attr-defined]

        # The child must not re-enter this runner's runtestloop, and must not emit the
        # parent's terminal output — it reports only through the pickle file.
        self.config.pluginmanager.unregister(name="parallel-runner")
        collected: list[tuple[str, object]] = []

        def _record(report: TestReport) -> None:
            collected.append((report.when or "call", report._to_json()))

        self.config.pluginmanager.register(_ReportCollector(_record), "parallel-report-collector")

        # The controller owns the session fixtures it set up pre-fork; this child must not
        # finalize its inherited copies. See session_scope.detach_in_child.
        detach_in_child(self.config._parallex_session)  # type: ignore[attr-defined]

        trace = os.environ.get("PARALLEX_TRACE")

        while True:
            with claim.get_lock():
                index = claim.value
                claim.value += 1
            if index >= len(groups):
                break
            group = groups[index]
            if trace:
                _trace(f"{worker_id} claimed group {index} ({len(group)} tests) t={time.monotonic():.2f}")
            for i, item in enumerate(group):
                # nextitem=None at a group's end tears down every scope above the item,
                # session included — which would make the next group this worker claims
                # rebuild it. That is safe only because detach_in_child() already emptied
                # the Session's finalizer list: the unwind reaches the Session and finds
                # nothing to run, so cached_result survives for the next group. Module and
                # class scopes still tear down properly, which is what we want.
                nextitem = group[i + 1] if i + 1 < len(group) else None
                item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            if trace:
                _trace(f"{worker_id} finished group {index} t={time.monotonic():.2f}")

        with open(path, "wb") as fh:
            pickle.dump(collected, fh, protocol=pickle.HIGHEST_PROTOCOL)
        return 0

    def _collect_reports(self, session: pytest.Session, children: list[_Child]) -> int:
        """Wait for every child, then replay its reports on the parent's hooks."""
        from _pytest.reports import TestReport

        failed = 0
        for child in children:
            _, status = os.waitpid(child.pid, 0)
            try:
                with open(child.report_path, "rb") as fh:
                    payload = pickle.load(fh)
            except (OSError, EOFError, pickle.UnpicklingError):
                payload = []
            finally:
                _unlink_quietly(child.report_path)

            for _when, raw in payload:
                report = TestReport._from_json(raw)
                session.config.hook.pytest_runtest_logreport(report=report)
                if report.failed:
                    failed += 1

            if status != 0:
                # A worker killed mid-group (OOM, segfault, a test calling os._exit)
                # takes its claimed group's remaining tests with it. Those tests never
                # report, and a runner that lets tests vanish silently is worse than
                # useless — so this is loud and it fails the run.
                sys.stderr.write(
                    f"\npytest-parallex: worker {child.worker_id} died (wait status "
                    f"{status}) after reporting {len(payload)} result(s); tests it had "
                    f"claimed but not finished are MISSING from this run\n"
                )
                failed += 1
        return failed


class _ReportCollector:
    """Captures reports inside a child instead of printing them."""

    def __init__(self, record) -> None:
        self._record = record

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report: TestReport) -> None:
        self._record(report)


def _trace(msg: str) -> None:
    """Write straight to fd 2, bypassing pytest's capture (which the child inherits)."""
    os.write(2, f"[parallex] {msg}\n".encode())


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
