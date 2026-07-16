"""Integration tests for the async runner.

The async runner is the thread runner driven from an event loop, so its fixture behaviour
is covered by the parametrised regressions in test_regressions.py (which run every mode).
These cover the async-specific surface, and assert on what happened rather than on the
summary line — see the header of test_thread.py for why that distinction is the point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import suite

if TYPE_CHECKING:
    import pytest


class TestAsyncBasic:
    def test_tests_actually_run_on_workers(self, pytester: pytest.Pytester) -> None:
        """Not serial-in-disguise: async workers report 'a0'/'a1', a serial run 'main'."""
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
        result = pytester.runpytest("--parallel=async", "--parallel-workers=2")
        result.assert_outcomes(passed=2)

        who = set((pytester.path / "who").read_text().split())
        assert who, "no worker wrote its id"
        assert "main" not in who, f"ran serially: {who}"
        assert all(w.startswith("a") for w in who), who

    def test_reports_failures(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): assert False")
        result = pytester.runpytest("--parallel=async", "--parallel-workers=1")
        result.assert_outcomes(failed=1)
        assert result.ret == 1

    def test_empty_suite(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=async", "--parallel-workers=2")
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_every_test_runs_exactly_once(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(
            """
            import pytest

            @pytest.mark.parametrize("n", range(12))
            def test_n(n):
                with open("ran", "a") as fh:
                    fh.write(f"{n}\\n")
            """
        )
        result = pytester.runpytest("--parallel=async", "--parallel-workers=4")
        result.assert_outcomes(passed=12)
        ran = (pytester.path / "ran").read_text().split()
        assert sorted(map(int, ran)) == list(range(12)), sorted(ran)

    def test_session_fixture_body_runs_once(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(
            suite(
                """
                import time

                import pytest

                @pytest.fixture(scope="session")
                def shared():
                    time.sleep(0.2)
                    with open("setups", "a") as fh:
                        fh.write("x")
                    return 42
                """,
                *(f"def test_{i}(shared): assert shared == 42" for i in range(8)),
            )
        )
        result = pytester.runpytest("--parallel=async", "--parallel-workers=4")
        result.assert_outcomes(passed=8)
        assert len((pytester.path / "setups").read_text()) == 1
