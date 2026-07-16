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


class TestForkFillsWorkers:
    """A big module of function-fixture tests must use more than one worker.

    fork dealt work by module, so parallelism was capped at ``min(workers, modules)`` — one
    big file ran on a single worker, i.e. serially, while xdist spread the same tests over
    every core. ``plan_fork_claims`` deals a module that keeps no coarse fixture alive as
    individual tests, so they fill the machine. A module that owns a module/class fixture
    still stays whole; that half is ``test_a_module_fixture_module_is_not_split`` below.

    Asserting on the count of distinct workers, because "did it parallelise" is invisible in
    the summary — a serial run and a fully-spread run both print the same "N passed".
    """

    def test_a_pure_function_module_spreads_across_workers(self, pytester):
        pytester.makepyfile(
            """
            import pytest

            @pytest.fixture
            def f():
                return 1

            @pytest.mark.parametrize("n", range(16))
            def test_n(n, f, worker_id):
                with open("workers", "a") as fh:
                    fh.write(worker_id + "\\n")
            """
        )
        result = pytester.runpytest("--parallel=fork", "--parallel-workers=8")
        result.assert_outcomes(passed=16)

        workers = set((pytester.path / "workers").read_text().split())
        assert len(workers) > 1, (
            f"16 tests in one module ran on {workers} — fork used a single worker, i.e. serial"
        )

    def test_a_module_fixture_module_is_not_split(self, pytester):
        """The other half: a module with a module fixture stays on one worker, built once."""
        pytester.makepyfile(
            """
            import pytest

            @pytest.fixture(scope="module")
            def heavy():
                with open("builds", "a") as fh:
                    fh.write("x")
                return 1

            @pytest.mark.parametrize("n", range(12))
            def test_n(n, heavy):
                assert heavy == 1
            """
        )
        result = pytester.runpytest("--parallel=fork", "--parallel-workers=8")
        result.assert_outcomes(passed=12)

        builds = (pytester.path / "builds").read_text()
        assert len(builds) == 1, (
            f"module fixture built {len(builds)}x — the module was split when it must stay whole"
        )


class TestFunctionFixtureIsolation:
    """The bug that shipped in 0.1.1-0.1.4 and that the old suite could not see.

    ``FixtureDef`` keeps one ``cached_result`` and one ``_finalizers`` list, shared across
    threads. For a *function*-scoped fixture — the overwhelmingly common kind — two workers
    running two tests share that one slot, so one thread's setup runs while another's
    teardown clears it: pytest dies with a bare ``AssertionError`` inside ``execute``, or a
    fixture just fails to set up. The old test used 2 tests on 2 workers and got lucky on
    timing; the plugin was unsound for ``@pytest.fixture`` the whole time.

    Fixed by making those two attributes per-thread for function scope (session/module
    stay shared — that is the feature). These assert on it directly: each test checks it
    got *its own* fixture value, and that setups and teardowns balance.
    """

    @pytest.mark.parametrize("mode", ["thread", "async"])
    def test_each_test_gets_its_own_function_fixture(self, pytester, mode):
        pytester.makepyfile(
            """
            import time

            import pytest

            @pytest.fixture
            def res(request):
                name = request.node.name
                with open("setups", "a") as fh:
                    fh.write("x")
                yield name
                with open("teardowns", "a") as fh:
                    fh.write("x")

            @pytest.mark.parametrize("n", range(24))
            def test_n(n, res):
                time.sleep(0.01)              # widen the window for the race
                assert res == f"test_n[{n}]"  # must be THIS test's value, not a neighbour's
            """
        )
        result = pytester.runpytest(f"--parallel={mode}", "--parallel-workers=6")
        # A single cross-thread bleed flips one of these asserts, so passed=24 is the claim.
        result.assert_outcomes(passed=24)

        setups = len((pytester.path / "setups").read_text())
        teardowns = len((pytester.path / "teardowns").read_text())
        assert setups == 24, f"{setups} setups, expected one per test"
        assert teardowns == 24, f"{teardowns} teardowns, expected one per test"


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
        assert counter.read_text() == "3", f"testsfailed={counter.read_text()} for 3 failing tests"


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


class TestWorkerScopedFixtures:
    def test_server_is_hoisted_but_per_worker_database_is_not(self, pytester):
        """The split the whole feature exists to make possible.

        One server in the controller, one database per worker on it — what xdist's
        filelock recipe exists to fake. It works because requesting ``worker_id`` marks a
        fixture per-worker, so ``database_url`` is left to the workers while
        ``postgres_server`` is hoisted.

        Both halves must hold at once, and each fails differently:

        * hoist ``database_url`` too and all workers share one database, then a per-test
          create_all/drop_all wipes its siblings mid-test.
        * hoist neither and every worker boots its own server — xdist's behaviour, and
          the reason this project exists.

        The value matters as much as the marking: while xdist's ``worker_id`` fixture
        shadowed ours, ``database_url`` was correctly *not* hoisted and then every worker
        resolved ``worker_id`` to xdist's ``'master'`` and carved the same database
        anyway. Asserting on distinct names is what catches that; asserting on
        pass/fail does not.
        """
        pytester.makeconftest(
            """
            import pytest

            @pytest.fixture(scope="session")
            def postgres_server():
                with open("servers", "a") as fh:
                    fh.write("1")
                return "postgres://host"

            @pytest.fixture(scope="session")
            def database_url(worker_id, postgres_server):
                with open("databases", "a") as fh:
                    fh.write(worker_id + "\\n")
                return f"{postgres_server}/test_{worker_id}"
            """
        )
        pytester.makepyfile(
            **{
                f"test_{n}": f"def test_{n}(database_url): assert database_url"
                for n in ("alpha", "beta", "gamma", "delta")
            }
        )
        result = pytester.runpytest("--parallel=fork", "--parallel-workers=4")
        result.assert_outcomes(passed=4)

        servers = (pytester.path / "servers").read_text()
        assert len(servers) == 1, f"server booted {len(servers)}x — it should be hoisted"

        used = [w for w in (pytester.path / "databases").read_text().split() if w]
        assert used, "database_url never ran"
        assert "master" not in used, "resolved xdist's worker_id, not ours"
        assert len(used) == len(set(used)), (
            f"workers shared a database: {used} — database_url was hoisted"
        )
        assert all(w.startswith("f") for w in used), used


class TestHoistingUnderStrictWarnings:
    def test_hoisting_survives_filterwarnings_error(self, pytester):
        """Hoisting must still work for a suite that turns warnings into errors.

        The controller hoists by borrowing ``session.items[0]``'s request and calling
        ``getfixturevalue`` from ``pytest_runtestloop``. pytest decides whether that counts
        as "during teardown" by checking ``request.node in setupstate.stack`` — and the
        borrowed item never ran, so it wasn't there. pytest 9 warns
        (``PytestRemovedIn10Warning``) and carries on.

        Under ``filterwarnings = error`` that warning *is* an exception. It landed in
        ``setup_session_fixtures``' except branch, so every hoist was recorded as failed and
        left to the workers: the container booted once per worker, exactly like xdist, on a
        green run with nothing in the summary. Anyone with that setting had installed this
        plugin and got nothing from it.

        pytest's own TODO says 10.1 drops the condition and raises unconditionally, which
        makes that everyone's behaviour. Asserting on the boot count rather than on the
        warning keeps this honest either way: the test fails if the feature stops working,
        whatever pytest decides to call it.
        """
        pytester.makeini(
            """
            [pytest]
            filterwarnings =
                error
            """
        )
        pytester.makeconftest(
            """
            import pytest

            @pytest.fixture(scope="session")
            def container():
                with open("boots", "a") as fh:
                    fh.write("1")
                return "postgres://x"
            """
        )
        pytester.makepyfile(
            **{
                f"test_{n}": f"def test_{n}(container): assert container"
                for n in ("alpha", "beta", "gamma", "delta")
            }
        )
        result = pytester.runpytest("--parallel=fork", "--parallel-workers=4")
        result.assert_outcomes(passed=4)

        boots = (pytester.path / "boots").read_text()
        assert len(boots) == 1, (
            f"container booted {len(boots)}x under filterwarnings=error — "
            f"hoisting silently degraded to once-per-worker"
        )


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
        result = pytester.runpytest_subprocess("--parallel=fork", "--parallel-workers=3", "-s")
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
