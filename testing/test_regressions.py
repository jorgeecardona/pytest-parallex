"""Regressions for bugs that a green suite did not catch.

Every test here failed at some point against code whose own unit tests passed. They share
a shape: the old tests asserted on *counts* from tiny suites ("2 passed") in the one
configuration that happened to work, and each of these bugs lives just outside that. So
they assert on things a count cannot see — how many times a fixture body ran, whether a
worker is who it says it is, whether output survived a fork — and they run the modes
against each other, because four runners that disagree cannot all be right.
"""

from __future__ import annotations

import textwrap

import pytest


def _suite(header: str, *body: str) -> str:
    """Build a test module source.

    ``makepyfile`` dedents by common leading prefix, which a zero-indented line appended
    to an indented block silently defeats — the prefix becomes '' and the block keeps its
    indentation, so the module is a syntax error rather than a test.
    """
    return textwrap.dedent(header).strip() + "\n\n" + "\n".join(body) + "\n"

# Modes that share one interpreter, so `scope="session"` genuinely means once. `process`
# is excluded on purpose: a fresh interpreter per worker re-runs session fixtures by
# construction, which is the xdist behaviour `fork` exists to fix.
SHARED_SESSION_MODES = ["thread", "async", "fork"]
ALL_MODES = ["thread", "process", "async", "fork"]


class TestFixtureCaching:
    @pytest.mark.parametrize("mode", SHARED_SESSION_MODES)
    def test_session_fixture_body_runs_exactly_once(self, pytester, mode):
        """A cached non-function-scoped fixture must not be rebuilt, or crash the run.

        ``ThreadFixtureLocking`` used to short-circuit a cache hit by returning from a
        pluggy ``wrapper=True`` hookimpl without yielding, which pluggy rejects outright
        ("wrap_controller ... did not yield"). It needed a *second* thread to arrive after
        the cache was warm, so the two-test suites in test_thread.py never triggered it.
        """
        pytester.makepyfile(
            _suite(
                """
                import time

                import pytest

                # A relative path, so it lands in the run's own rootdir where the calling
                # test can find it. tmp_path_factory's basetemp is somewhere under
                # /tmp/pytest-of-*, which is not the pytester directory.
                #
                # The sleep is the point of the test, not incidental slowness. pytest
                # checks the fixture cache in FixtureDef.execute(), *before* the setup
                # hook fires, so a fixture that returns instantly is cached before any
                # other worker reaches the hook and the contended path never runs — the
                # bug this guards hid behind exactly that. Sleeping holds the first worker
                # inside the hook long enough for the others to queue on the lock, which
                # is the state the code under test exists to handle.
                @pytest.fixture(scope="session")
                def expensive():
                    time.sleep(0.3)
                    with open("setup_count", "a") as fh:
                        fh.write("x")
                    return 42
                """,
                *(f"def test_{i}(expensive): assert expensive == 42" for i in range(12)),
            )
        )
        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=4")
        result.stdout.fnmatch_lines(["*12 passed*"])
        assert result.ret == 0

        counter = pytester.path / "setup_count"
        assert counter.exists(), "the session fixture never ran at all"
        body_runs = len(counter.read_text())
        assert body_runs == 1, f"session fixture body ran {body_runs}x, expected once"

    @pytest.mark.parametrize("mode", SHARED_SESSION_MODES)
    def test_failing_session_fixture_reports_to_every_waiter(self, pytester, mode):
        """A fixture that raises must raise for the threads that waited on it too.

        The cache entry holds an exception rather than a value; a short-circuit that
        returns ``cached_result[0]`` regardless hands those waiters ``None`` and their
        tests fail somewhere confusing, or pass.
        """
        pytester.makepyfile(
            _suite(
                """
                import pytest

                @pytest.fixture(scope="session")
                def broken():
                    raise RuntimeError("fixture is broken")
                """,
                *(f"def test_{i}(broken): pass" for i in range(6)),
            )
        )
        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=3")
        result.stdout.fnmatch_lines(["*6 error*"])
        assert result.ret != 0


class TestTeardownOrdering:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_workers_do_not_tear_down_each_others_scopes(self, pytester, mode):
        """A worker's teardown must not reach into a scope another worker is inside.

        pytest decides what to tear down after an item by comparing it to ``nextitem``.
        The async runner used to run one task per *test* and pass the globally-next item
        as ``nextitem`` — but ``to_thread`` hands tasks to a shared pool, so that item
        usually belonged to a different worker. Threads then unwound each other's module
        scopes and pytest raised ``AssertionError: previous item was not torn down
        properly`` from a thread that had done nothing wrong.

        Needs *several modules* to show up: with one module every item shares module
        scope, nothing is torn down between items, and the bug stays invisible. Every
        single-file test in this suite missed it.
        """
        pytester.makeconftest(
            """
            import pytest

            @pytest.fixture(scope="session")
            def session_res():
                return "s"
            """
        )
        files = {}
        for name in ("alpha", "beta", "gamma", "delta"):
            files[f"test_{name}"] = _suite(
                f"""
                import pytest

                @pytest.fixture(scope="module")
                def mod_res_{name}():
                    yield "{name}"
                """,
                *(
                    f"def test_{name}_{i}(mod_res_{name}, session_res):\n"
                    f"    assert mod_res_{name} == '{name}' and session_res == 's'"
                    for i in range(3)
                ),
            )
        pytester.makepyfile(**files)

        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=4")
        result.assert_outcomes(passed=12)
        assert result.ret == 0


class TestReportingAgreement:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_outcomes_match_serial(self, pytester, mode):
        """Every mode must report what a serial run reports.

        xfail is the load-bearing case: an expected failure arrives as
        ``report.failed`` and is only *not* a failure because ``wasxfail`` is set. Any
        runner counting ``report.failed`` itself turns each xfail into a real failure —
        and any runner counting on top of pytest's own bookkeeping double-counts.
        """
        pytester.makepyfile(
            """
            import pytest

            def test_pass_1(): assert True
            def test_pass_2(): assert True
            def test_fail_1(): assert False
            def test_fail_2(): assert 1 == 2

            @pytest.mark.xfail(reason="known")
            def test_xfail(): assert False

            @pytest.mark.xfail(reason="fixed")
            def test_xpass(): assert True

            @pytest.mark.skip(reason="nope")
            def test_skip(): pass
            """
        )
        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=3")
        # Exactly the serial outcome: the two real failures, and nothing else promoted.
        result.assert_outcomes(passed=2, failed=2, skipped=1, xfailed=1, xpassed=1)
        assert result.ret == 1


class TestFailureCounting:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_testsfailed_matches_the_number_of_failing_tests(self, pytester, mode):
        """``session.testsfailed`` must equal the failures, not a multiple of them.

        Replaying a report through ``pytest_runtest_logreport`` already increments
        ``testsfailed``, because ``Session`` implements that hook itself. A runner that
        also counts ``report.failed`` while replaying reports every failure twice: this
        ran with ``testsfailed == 6`` for three failing tests, and printed 'FFFFFF'
        because the forked children still owned a terminal reporter and wrote their own
        progress alongside the controller's replay.

        Nothing user-facing said so — the exit code is 1 either way — so it needed
        sessionfinish to read the counter directly.
        """
        pytester.makeconftest(
            """
            import os

            def pytest_sessionfinish(session, exitstatus):
                # Only the controller records. A spawned worker (--parallel=process) runs
                # this same conftest in the same cwd, and would otherwise race the
                # controller writing its own, smaller count to the same file.
                if os.environ.get("PARALLEX_REPORT_PATH"):
                    return
                with open("testsfailed", "w") as fh:
                    fh.write(str(session.testsfailed))
            """
        )
        pytester.makepyfile(
            test_one="def test_a(): assert False",
            test_two="def test_b(): assert False",
            test_three="def test_c(): assert False",
        )
        pytester.runpytest(f"--parallel={mode}", "--parallel-workers=3")

        counter = pytester.path / "testsfailed"
        assert counter.exists(), "sessionfinish never ran in the controller"
        assert counter.read_text() == "3", (
            f"testsfailed={counter.read_text()} for 3 failing tests"
        )


class TestWorkerIdentity:
    @pytest.mark.parametrize("mode", ALL_MODES)
    def test_worker_id_is_ours_not_xdists(self, pytester, mode):
        """``parallex_worker_id`` must report a parallex worker.

        pytest-xdist ships a ``worker_id`` fixture, and a suite that parallelises is
        exactly the suite that has xdist installed too. While this plugin also called its
        fixture ``worker_id``, xdist's won on registration order and every test quietly
        got xdist's ``'master'`` — right shape, wrong answer, no error. This asserts on
        the prefix, so a regression to a shared name fails here rather than in a user's
        suite.
        """
        pytester.makepyfile(
            """
            def test_reports_worker(parallex_worker_id):
                with open("seen_worker", "a") as fh:
                    fh.write(parallex_worker_id + "\\n")
            """
        )
        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=2")
        result.stdout.fnmatch_lines(["*1 passed*"])

        record = pytester.path / "seen_worker"
        assert record.exists(), "the fixture never resolved"
        seen = set(record.read_text().split())
        assert seen != {"master"}, "got xdist's worker_id fixture, not ours"
        assert all(w[0] in "wfa" and w[1:].isdigit() for w in seen), seen


class TestForkOutput:
    def test_controller_output_is_not_duplicated_into_children(self, pytester):
        """Output written before the fork must appear once, not once per child.

        fork() copies the process, and with it any *unflushed* stdio buffer — so the
        controller's pending output is duplicated into every child and written for real
        when the child next flushes. A tty hides this completely (line-buffered, so the
        buffer is empty at fork); a pipe is block-buffered, so it reproduces under CI,
        `| tee`, or any redirect to a file.

        runpytest_subprocess is essential here: it gives the run a pipe for stdout, which
        is the whole precondition. In-process, this bug is invisible.
        """
        pytester.makepyfile(
            conftest="""
            def pytest_parallex_setup(config):
                print("CONTROLLER_MARKER")
                return {}
            """
        )
        pytester.makepyfile(
            test_a="def test_a(): assert True",
            test_b="def test_b(): assert True",
            test_c="def test_c(): assert True",
        )
        result = pytester.runpytest_subprocess(
            "--parallel=fork", "--parallel-workers=3", "-s"
        )
        result.stdout.fnmatch_lines(["*3 passed*"])
        assert result.stdout.str().count("CONTROLLER_MARKER") == 1


class TestProcessArgForwarding:
    def test_user_args_reach_the_worker(self, pytester):
        """The worker must run with the flags the user actually passed.

        ``-x`` is the visible probe: forwarded, the worker stops at its first failure and
        the second test never reports. The old runner forwarded only ``-v`` and ``--tb``,
        so -x, -k, -m, --maxfail, --strict-markers and every plugin option were dropped
        and workers ran a different configuration than the one asked for.
        """
        pytester.makepyfile(
            """
            def test_a_fails(): assert False
            def test_b_also_fails(): assert False
            """
        )
        result = pytester.runpytest("--parallel=process", "--parallel-workers=1", "-x")
        result.assert_outcomes(failed=1)

    def test_parallel_flags_are_not_forwarded(self, pytester):
        """A worker must not re-read --parallel, or it fans out again, recursively."""
        pytester.makepyfile("def test_a(): assert True")
        result = pytester.runpytest(
            "--parallel=process", "--parallel-workers", "2", "--parallex-no-session-scope"
        )
        result.stdout.fnmatch_lines(["*1 passed*"])
        assert result.ret == 0
