"""Integration tests for the thread runner.

These assert on what the runner *did*, not on what pytest printed. The distinction is the
whole reason this file was rewritten: the previous version asserted `"*2 passed*"` after
`--parallel=thread`, and `"2 passed"` is the same string a serial run prints. Neutering
every runner so the plugin did nothing at all left 32 of the 33 original tests passing —
which is exactly the failure `docs/benchmarks.md` records as trap 1, where the runner
silently never ran and the suite stayed green for hours.

The rule each test here is written against: **if the plugin did nothing, would this
assertion still pass?** If yes, the assertion is worthless, however green it looks.
Asserting on output is fine when the output *is* the product (see `test_reports_failures`);
it is worthless as evidence that something invisible happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import suite

if TYPE_CHECKING:
    import pytest


class TestThreadBasic:
    def test_tests_actually_run_on_workers(self, pytester: pytest.Pytester) -> None:
        """The runner must actually run — not hand the loop back to pytest.

        `pytest_runtestloop` is a firstresult hook, so a registration mistake makes pytest's
        builtin win: every test still passes, serially, and the only tell is CPU
        utilisation. `worker_id` is 'main' in a serial run and 'w0'/'w1' under this runner,
        so asking each test who ran it distinguishes the two. Nothing about a summary line
        can.
        """
        pytester.makepyfile(
            """
            def test_a(worker_id):
                with open("who", "a") as fh:
                    fh.write(worker_id + "\\n")

            def test_b(worker_id):
                with open("who", "a") as fh:
                    fh.write(worker_id + "\\n")
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        result.assert_outcomes(passed=2)

        who = set((pytester.path / "who").read_text().split())
        assert who, "no test recorded a worker"
        assert "main" not in who, f"tests ran serially — the runner never took the loop: {who}"
        assert all(w.startswith("w") for w in who), who

    def test_reports_failures(self, pytester: pytest.Pytester) -> None:
        """A failing test must be reported as failed, with a non-zero exit.

        Asserting on the outcome is right here: reporting faithfully *is* a runner's
        contract, so this is the product rather than a proxy for it.
        """
        pytester.makepyfile("def test_a(): assert False")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        result.assert_outcomes(failed=1)
        assert result.ret == 1

    def test_mixed_results(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(
            """
            def test_pass(): assert True
            def test_fail(): assert False
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        result.assert_outcomes(passed=1, failed=1)
        assert result.ret == 1

    def test_empty_suite(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_every_test_runs_exactly_once(self, pytester: pytest.Pytester) -> None:
        """No test may be dropped or run twice.

        A work-distribution bug is far more likely to double-run or skip an item than to
        crash, and a count in the summary hides both: 3 tests where one ran twice and
        another not at all still prints "3 passed".
        """
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.parametrize("n", range(12))
            def test_n(n):
                with open("ran", "a") as fh:
                    fh.write(f"{n}\\n")
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=4")
        result.assert_outcomes(passed=12)

        ran = (pytester.path / "ran").read_text().split()
        assert sorted(map(int, ran)) == list(range(12)), f"each test exactly once: {sorted(ran)}"


class TestThreadFixtures:
    def test_session_fixture_body_runs_once(self, pytester: pytest.Pytester) -> None:
        """Counts executions of the fixture *body*, not what the fixture returned.

        The old version kept a module-global counter and asserted the fixture's value was
        1 — which a serial run satisfies too, so it proved nothing about the runner. The
        body appending to a file is the actual observation: 4 workers that each build their
        own leave 4 bytes.
        """
        pytester.makepyfile(
            suite(
                """
                import time

                import pytest

                @pytest.fixture(scope="session")
                def shared():
                    time.sleep(0.2)          # widen the window so workers really contend
                    with open("setups", "a") as fh:
                        fh.write("x")
                    return 42
                """,
                *(f"def test_{i}(shared): assert shared == 42" for i in range(8)),
            )
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=4")
        result.assert_outcomes(passed=8)

        setups = (pytester.path / "setups").read_text()
        assert len(setups) == 1, f"session fixture body ran {len(setups)}x, expected once"

    def test_function_fixture_runs_per_test(self, pytester: pytest.Pytester) -> None:
        """The mirror of the above: a function-scoped fixture must NOT be shared."""
        pytester.makepyfile(
            suite(
                """
                import pytest

                @pytest.fixture
                def per_test():
                    with open("setups", "a") as fh:
                        fh.write("x")
                    return object()
                """,
                *(f"def test_{i}(per_test): assert per_test" for i in range(6)),
            )
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=3")
        result.assert_outcomes(passed=6)

        setups = (pytester.path / "setups").read_text()
        assert len(setups) == 6, f"function fixture ran {len(setups)}x, expected once per test"

    def test_yield_fixture_tears_down(self, pytester: pytest.Pytester) -> None:
        """Teardown must actually run — the half everyone forgets to assert."""
        pytester.makepyfile(
            """
            import pytest

            @pytest.fixture
            def resource():
                yield "value"
                with open("teardowns", "a") as fh:
                    fh.write("x")

            def test_a(resource): assert resource == "value"
            def test_b(resource): assert resource == "value"
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        result.assert_outcomes(passed=2)
        assert len((pytester.path / "teardowns").read_text()) == 2


class TestThreadConcurrency:
    def test_threads_really_overlap(self, pytester: pytest.Pytester) -> None:
        """Tests must actually run at the same time.

        The old version of this test collected `threading.current_thread().ident` into a
        set, slept to create overlap, and then asserted `"*4 passed*"` — it built the
        evidence and threw it away, so it passed on one thread, serially, with the plugin
        uninstalled.

        Overlap is the real claim, so measure it: each test records the interval it was
        inside. If any two intervals intersect, work genuinely happened at the same time.
        Distinct thread ids alone would not prove it — a pool can reuse one thread
        sequentially.
        """
        pytester.makepyfile(
            """
            import time

            import pytest

            @pytest.mark.parametrize("n", range(4))
            def test_n(n):
                start = time.monotonic()
                time.sleep(0.3)
                with open("spans", "a") as fh:
                    fh.write(f"{start} {time.monotonic()}\\n")
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=4")
        result.assert_outcomes(passed=4)

        spans = [
            tuple(map(float, line.split()))
            for line in (pytester.path / "spans").read_text().splitlines()
            if line.strip()
        ]
        assert len(spans) == 4
        overlapping = any(
            a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1 :]
        )
        assert overlapping, f"no two tests ran at the same time — this was serial: {spans}"

    def test_maxfail_stops_the_run(self, pytester: pytest.Pytester) -> None:
        """--maxfail must actually stop work, not just be reported.

        The old version asserted "*1 failed*", which is what you get whether the run
        stopped or simply had one failure. What makes it a stop is the tests that never
        ran, so count those.
        """
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.parametrize("n", range(10))
            def test_n(n):
                with open("ran", "a") as fh:
                    fh.write("x")
                assert False
            """
        )
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1", "--maxfail=1")
        ran = len((pytester.path / "ran").read_text())
        assert ran < 10, f"--maxfail=1 did not stop the run: all {ran} tests executed"
        assert result.ret != 0
