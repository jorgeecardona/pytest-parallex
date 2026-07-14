"""Process-based parallel test runner.

Uses ProcessPoolExecutor to run tests in separate processes.
Each worker process runs its own pytest session with a subset of tests.
Reports are serialized and replayed on the main process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

import pytest

from pytest_parallex.scheduler import schedule_round_robin


class ProcessRunner:
    def __init__(self, config: pytest.Config, num_workers: int) -> None:
        self.config = config
        self.num_workers = num_workers

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        data = self.config.hook.pytest_parallex_setup(config=self.config)
        self.config._parallel_setup_data = data or {}  # type: ignore[attr-defined]

    @pytest.hookimpl
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        if session.testsfailed and not session.config.option.continue_on_collection_errors:
            raise session.Interrupted(
                "%d error%s during collection"
                % (session.testsfailed, "s" if session.testsfailed != 1 else "")
            )

        if not session.items:
            return True

        # Divide tests into node ID lists
        nodeids = [item.nodeid for item in session.items]
        chunks = schedule_round_robin(nodeids, self.num_workers)

        rootdir = str(session.config.rootpath)
        base_args = _get_base_args(session.config)

        failed = False
        with ProcessPoolExecutor(max_workers=self.num_workers) as executor:
            futures = []
            for i, chunk in enumerate(chunks):
                future = executor.submit(
                    _run_worker_subprocess,
                    worker_id=f"w{i}",
                    nodeids=chunk,
                    rootdir=rootdir,
                    base_args=base_args,
                )
                futures.append(future)

            for future in as_completed(futures):
                returncode = future.result()
                if returncode != 0:
                    failed = True

        if failed:
            session.testsfailed = 1

        return True

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        data = getattr(self.config, "_parallel_setup_data", {})
        self.config.hook.pytest_parallex_teardown(config=self.config, data=data)


def _get_base_args(config: pytest.Config) -> list[str]:
    """Extract base pytest args, stripping parallel-specific options."""
    args = []
    # Pass through common options
    if config.option.verbose:
        args.append("-v")
    if getattr(config.option, "tb", None):
        args.extend(["--tb", config.option.tb])
    return args


def _run_worker_subprocess(
    worker_id: str,
    nodeids: list[str],
    rootdir: str,
    base_args: list[str],
) -> int:
    """Run a subset of tests in a subprocess.

    Uses subprocess instead of multiprocessing to get a clean Python
    interpreter without any shared state issues.
    """
    env = os.environ.copy()
    env["PYTEST_PARALLEL_WORKER"] = worker_id

    cmd = [sys.executable, "-m", "pytest"] + base_args + nodeids
    result = subprocess.run(cmd, cwd=rootdir, env=env)
    return result.returncode
