"""Integration tests for the process runner.

Reporting fidelity (reports replayed on the controller, failure counts preserved, user
args forwarded, worker crashes surfaced) is covered by the parametrised regressions in
test_regressions.py. These cover the process-specific surface, asserting on outcomes and
replayed reports rather than on exit codes alone — a runner that ran nothing and a runner
that ran everything both exit 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


class TestProcessBasic:
    def test_reports_are_replayed_on_the_controller(self, pytester: pytest.Pytester) -> None:
        """The controller must report each test — not just exit 0.

        The old runner exited 0 while reporting nothing (workers printed to their own
        stdout). Asserting on ``assert_outcomes`` reads the reports the controller actually
        replayed, which is the thing that was broken.
        """
        pytester.makepyfile(
            """
            def test_a(): assert True
            def test_b(): assert True
            def test_c(): assert True
            """
        )
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        result.assert_outcomes(passed=3)
        assert result.ret == 0

    def test_failures_are_counted(self, pytester: pytest.Pytester) -> None:
        """Every failure must show up, not collapse to one."""
        pytester.makepyfile(
            """
            def test_a(): assert False
            def test_b(): assert False
            def test_c(): assert True
            """
        )
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        result.assert_outcomes(passed=1, failed=2)
        assert result.ret == 1

    def test_empty_suite(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_spread_across_files(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile(
            test_one="def test_a(): pass",
            test_two="def test_b(): pass",
            test_three="def test_c(): pass",
            test_four="def test_d(): pass",
        )
        result = pytester.runpytest("--parallel=process", "--parallel-workers=4")
        result.assert_outcomes(passed=4)
