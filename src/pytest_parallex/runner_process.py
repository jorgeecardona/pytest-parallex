"""Process-based parallel test runner — spawn a fresh interpreter per worker.

Each worker is a plain ``python -m pytest <nodeids>`` subprocess running its own session,
so nothing is shared and nothing needs to be fork-safe. That is the point of this mode:
it is the one that works when ``--parallel=fork`` refuses (threads at import, a live
connection, Windows) and when tests mutate process-global state.

The cost is the one ``--parallel=fork`` exists to avoid — every worker re-imports and
re-collects the whole suite before it runs a single test. Prefer fork when the process is
quiet enough to allow it.

Workers report through ``_reports`` rather than their own stdout, so the controller prints
one summary and one exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import pytest

from pytest_parallex._loop import raise_if_collection_failed
from pytest_parallex._reports import (
    ENV_REPORT_PATH,
    read_reports,
    replay,
    unlink_quietly,
)
from pytest_parallex.scheduler import schedule_round_robin


@dataclass(frozen=True)
class _WorkerResult:
    worker_id: str
    returncode: int
    payload: list[Any]
    output: str


class ProcessRunner:
    def __init__(self, config: pytest.Config, num_workers: int) -> None:
        self.config = config
        self.num_workers = num_workers

    @pytest.hookimpl(trylast=True)
    def pytest_sessionstart(self) -> None:
        data = self.config.hook.pytest_parallex_setup(config=self.config)
        self.config._parallel_setup_data = data or {}  # type: ignore[attr-defined]

    @pytest.hookimpl
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        raise_if_collection_failed(session)

        if not session.items:
            return True

        nodeids = [item.nodeid for item in session.items]
        chunks = schedule_round_robin(nodeids, self.num_workers)

        self.config._parallex_stats = {  # type: ignore[attr-defined]
            "mode": "process",
            "workers": len(chunks),
        }
        rootdir = str(session.config.rootpath)
        base_args = _get_base_args(session.config)
        # -s means "don't capture" — so we don't. The output interleaves across workers,
        # which is what asking for raw output from a parallel run buys you; swallowing it
        # instead would make -s print nothing at all.
        capture = session.config.option.capture != "no"

        # Threads, not processes: every one of these does nothing but wait on a
        # subprocess. A ProcessPoolExecutor here would fork a worker whose whole job is to
        # call subprocess.run — a process spawned to spawn a process.
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            futures = [
                executor.submit(
                    _run_worker_subprocess,
                    worker_id=f"w{i}",
                    nodeids=chunk,
                    rootdir=rootdir,
                    base_args=base_args,
                    capture=capture,
                )
                for i, chunk in enumerate(chunks)
            ]

            # as_completed yields in *this* thread, so replay stays single-threaded and
            # the terminal reporter needs no lock.
            for future in as_completed(futures):
                result = future.result()
                replay(session.config, result.payload)

                if result.returncode not in (0, 1) or (
                    result.returncode == 1 and not result.payload
                ):
                    # 1 with reports is the ordinary "tests failed" path — already
                    # replayed above. Anything else means the worker itself broke
                    # (usage error, import error, crash), and its tests never reported.
                    # Silently dropping them would show a green run that tested nothing.
                    session.testsfailed += 1
                    sys.stderr.write(
                        f"\npytest-parallex: worker {result.worker_id} exited "
                        f"{result.returncode} after reporting {len(result.payload)} "
                        f"result(s); its remaining tests are MISSING from this run\n"
                        f"{result.output}\n"
                    )

        return True

    @pytest.hookimpl
    def pytest_sessionfinish(self) -> None:
        data = getattr(self.config, "_parallel_setup_data", {})
        self.config.hook.pytest_parallex_teardown(config=self.config, data=data)


#: Parallex's own options, which a worker must never see. Mapped to whether the value can
#: arrive as a following token (``--parallel fork``) as well as glued on (``--parallel=fork``).
_OWN_OPTIONS = {
    "--parallel": True,
    "--parallel-workers": True,
    "--parallex-no-session-scope": False,
}

#: Coverage options that belong to the controller's session alone. Workers keep ``--cov``
#: so their executed lines are measured, but reporting and gating happen exactly once, on
#: the controller's combined data. Same following-token convention as ``_OWN_OPTIONS``.
_CONTROLLER_ONLY_OPTIONS = {
    "--cov-report": True,
    "--cov-fail-under": True,
}


def _get_base_args(config: pytest.Config) -> list[str]:
    """The user's own pytest args, forwarded to a worker.

    Rebuilt from the real argv rather than hand-picked. The earlier version forwarded
    exactly ``-v`` and ``--tb``, which silently dropped everything else the user asked
    for: ``-x``, ``--maxfail``, ``-m``, ``-p``, ``--strict-markers``, every plugin's
    options. A worker configured differently from the controller is a worker whose results
    don't mean what the summary says they mean.

    Deliberately removed:

    * Parallex's own options. A worker that re-read ``--parallel`` would fan out again,
      and its workers would fan out again. Their absence is what terminates the recursion.
    * The positional test paths, which the caller replaces with explicit nodeids — leaving
      them in would make every worker run the whole suite.
    * Coverage reporting and gating. A worker keeps measuring (``--cov`` stays), but
      ``--cov-report=`` and ``--cov-fail-under=0`` are pinned so a worker never renders a
      report and never fails on its own subset's coverage — ``fail_under`` judged against
      one worker's slice of the suite fails suites whose combined coverage is fine.
    """
    positional = set(config.args)
    args: list[str] = []
    skip_next = False

    for token in config.invocation_params.args:
        if skip_next:
            skip_next = False
            continue
        name = token.split("=", 1)[0]
        if name in _OWN_OPTIONS:
            # A separate value token only follows when it wasn't glued on with '='.
            skip_next = _OWN_OPTIONS[name] and "=" not in token
            continue
        if name in _CONTROLLER_ONLY_OPTIONS:
            skip_next = _CONTROLLER_ONLY_OPTIONS[name] and "=" not in token
            continue
        if token in positional:
            continue
        args.append(token)

    # The workers would otherwise race each other writing one .pytest_cache.
    args.extend(["-p", "no:cacheprovider"])

    if getattr(config.option, "cov_source", None):
        args.extend(["--cov-report=", "--cov-fail-under=0"])
    return args


def _worker_environment(
    worker_id: str, report_path: str, base_environment: dict[str, str]
) -> dict[str, str]:
    """The subprocess environment for one worker, coverage-namespaced.

    Every worker session ends with pytest-cov combining ``$COVERAGE_FILE.*`` — on a shared
    namespace, N workers combine and delete each other's data files mid-combine
    (``sqlite3.OperationalError: no such table: other_db.file`` → INTERNALERROR exit 3,
    with that worker's remaining tests reported MISSING; timing-dependent, so it surfaced
    as a flaky CI task). Suffixing the worker id gives each worker a private namespace:
    ``.coverage.w3`` combines only ``.coverage.w3.*``, and the controller's own combine at
    session end sweeps up every ``.coverage.w*`` into the single gated, reported total.
    """
    environment = dict(base_environment)
    environment["PYTEST_PARALLEL_WORKER"] = worker_id
    environment[ENV_REPORT_PATH] = report_path
    coverage_file = environment.get("COVERAGE_FILE", ".coverage")
    environment["COVERAGE_FILE"] = f"{coverage_file}.{worker_id}"
    return environment


def _run_worker_subprocess(
    worker_id: str,
    nodeids: list[str],
    rootdir: str,
    base_args: list[str],
    *,
    capture: bool = True,
) -> _WorkerResult:
    """Run a subset of tests in a fresh interpreter and bring its reports home."""
    fd, report_path = tempfile.mkstemp(prefix=f"pytest-parallex-{worker_id}-", suffix=".reports")
    os.close(fd)

    env = _worker_environment(worker_id, report_path, dict(os.environ))

    cmd = [sys.executable, "-m", "pytest", *base_args, *nodeids]
    try:
        # Captured by default, not inherited: N workers writing to one terminal interleave
        # into noise, and the controller reprints all of it from the replayed reports
        # anyway. What we keep is only used to explain a worker that died before it could
        # report. Under -s the user has asked for the raw stream, so we let it through.
        # check=False: a non-zero exit (failing tests) is inspected by the caller.
        proc = subprocess.run(  # noqa: S603 — argv is sys.executable + our own args
            cmd,
            cwd=rootdir,
            env=env,
            capture_output=capture,
            text=True,
            check=False,
        )
        return _WorkerResult(
            worker_id=worker_id,
            returncode=proc.returncode,
            payload=read_reports(report_path),
            output=(proc.stdout or "") + (proc.stderr or "") if capture else "",
        )
    finally:
        unlink_quietly(report_path)
