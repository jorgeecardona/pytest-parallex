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


class TestCoverageUnderProcess:
    """--cov forwarded to workers must measure, and nothing else.

    Found on a real suite (coverage-gated, 250 tests): every worker session ran
    pytest-cov's session-end machinery on the shared data-file namespace. Workers
    combined and deleted each other's ``.coverage.*`` files mid-combine
    (``sqlite3.OperationalError: no such table: other_db.file`` → INTERNALERROR,
    exit 3, tests MISSING), and each worker applied ``fail_under`` to its own
    subset. The controller is the only session that may combine, report, or gate.
    """

    def test_worker_arguments_neutralize_reporting_and_gate(
        self, pytester: pytest.Pytester
    ) -> None:
        from pytest_parallex.runner_process import _get_base_args

        config = pytester.parseconfig(
            "--parallel=process",
            "--cov=somepkg",
            "--cov-report=term-missing",
            "--cov-fail-under=90",
            "tests",
        )
        base_args = _get_base_args(config)

        assert "--cov=somepkg" in base_args
        assert "--cov-report=term-missing" not in base_args
        assert "--cov-fail-under=90" not in base_args
        assert "--cov-report=" in base_args
        assert "--cov-fail-under=0" in base_args

    def test_worker_arguments_untouched_without_coverage(self, pytester: pytest.Pytester) -> None:
        from pytest_parallex.runner_process import _get_base_args

        config = pytester.parseconfig("--parallel=process", "tests")
        base_args = _get_base_args(config)

        assert not any(argument.startswith("--cov") for argument in base_args)

    def test_workers_get_their_own_coverage_file_namespace(self) -> None:
        from pytest_parallex.runner_process import _worker_environment

        environment = _worker_environment(
            "w3", "reports.path", {"COVERAGE_FILE": "/data/.coverage"}
        )
        assert environment["COVERAGE_FILE"] == "/data/.coverage.w3"

        environment = _worker_environment("w3", "reports.path", {})
        assert environment["COVERAGE_FILE"] == ".coverage.w3"

    def test_gate_applies_once_to_the_combined_run(self, pytester: pytest.Pytester) -> None:
        """Two workers each cover half a module; only the combined total may be gated.

        Serial coverage is 100%. Per-subset gating would fail both workers at
        fail-under=95; a combine race would INTERNALERROR. The run must pass, gate
        evaluated once on the controller's combined data.
        """
        pytester.makepyfile(
            covered_module="""
def left():
    return 1


def right():
    return 2
""",
            test_left="""
from covered_module import left


def test_left():
    assert left() == 1
""",
            test_right="""
from covered_module import right


def test_right():
    assert right() == 2
""",
        )
        result = pytester.runpytest_subprocess(
            "--parallel=process",
            "--parallel-workers=2",
            "--cov=covered_module",
            "--cov-fail-under=95",
            "-p",
            "no:cacheprovider",
        )
        result.assert_outcomes(passed=2)
        assert result.ret == 0
        combined_output = result.stdout.str() + result.stderr.str()
        assert "INTERNALERROR" not in combined_output
        assert "MISSING from this run" not in combined_output
