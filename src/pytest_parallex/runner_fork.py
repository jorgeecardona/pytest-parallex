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

import os
import pickle
import sys
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from pytest_parallex._safety import ForkUnsafe, assert_forkable
from pytest_parallex.scheduler import schedule_by_scope

if TYPE_CHECKING:
    from _pytest.reports import TestReport


@dataclass(frozen=True)
class _Child:
    """A forked worker and the file its serialized reports land in."""

    pid: int
    worker_id: str
    report_path: str
    num_items: int


class ForkRunner:
    def __init__(self, config: pytest.Config, num_workers: int) -> None:
        self.config = config
        self.num_workers = num_workers

    @pytest.hookimpl(trylast=True)
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

        try:
            assert_forkable()
        except ForkUnsafe as exc:
            raise pytest.UsageError(
                f"--parallel=fork requires a quiet process at fork time, but {exc}. "
                f"Move the offending setup into a fixture so it runs after the fork, or "
                f"use --parallel=process."
            ) from exc

        chunks = [c for c in schedule_by_scope(session.items, self.num_workers) if c]
        children = self._fork_children(session, chunks)
        failed = self._collect_reports(session, children)

        self.config.hook.pytest_parallex_teardown(
            config=self.config, data=self.config._parallel_setup_data
        )
        if failed:
            session.testsfailed += failed
        return True

    def _fork_children(self, session: pytest.Session, chunks: list[list[pytest.Item]]) -> list[_Child]:
        children: list[_Child] = []
        for i, chunk in enumerate(chunks):
            worker_id = f"f{i}"
            fd, path = tempfile.mkstemp(prefix=f"pytest-parallex-{worker_id}-", suffix=".reports")
            os.close(fd)

            pid = os.fork()
            if pid == 0:
                # --- child ---
                code = 1
                try:
                    code = self._run_chunk(session, chunk, worker_id, path)
                finally:
                    # os._exit: skip atexit/flush handlers, which belong to the parent's
                    # copy of the world and would double-run here.
                    os._exit(code)
            children.append(_Child(pid=pid, worker_id=worker_id, report_path=path, num_items=len(chunk)))
        return children

    def _run_chunk(
        self, session: pytest.Session, chunk: list[pytest.Item], worker_id: str, path: str
    ) -> int:
        """Run one chunk in the forked child and pickle its reports to ``path``."""
        self.config._parallel_worker_id = worker_id  # type: ignore[attr-defined]

        # The child must not re-enter this runner's runtestloop, and must not emit the
        # parent's terminal output — it reports only through the pickle file.
        self.config.pluginmanager.unregister(name="parallel-runner")
        collected: list[tuple[str, object]] = []

        def _record(report: TestReport) -> None:
            collected.append((report.when or "call", report._to_json()))

        self.config.pluginmanager.register(_ReportCollector(_record), "parallel-report-collector")

        for i, item in enumerate(chunk):
            nextitem = chunk[i + 1] if i + 1 < len(chunk) else None
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)

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

            if status != 0 and not payload:
                # A child that died without reporting would otherwise vanish silently —
                # the whole point of a test runner is that nothing disappears.
                sys.stderr.write(
                    f"\npytest-parallex: worker {child.worker_id} died (exit status {status}) "
                    f"with {child.num_items} test(s) unreported\n"
                )
                failed += 1
                continue

            for _when, raw in payload:
                report = TestReport._from_json(raw)
                session.config.hook.pytest_runtest_logreport(report=report)
                if report.failed:
                    failed += 1
        return failed


class _ReportCollector:
    """Captures reports inside a child instead of printing them."""

    def __init__(self, record) -> None:
        self._record = record

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report: TestReport) -> None:
        self._record(report)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
