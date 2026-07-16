"""Tests for plugin CLI options and configuration."""

from __future__ import annotations

import pytest


def test_dunder_version_matches_the_package_metadata():
    """``pytest_parallex.__version__`` must equal the installed version.

    The literal in __init__.py exists for humans (`pytest_parallex.__version__` at a
    REPL); pyproject stays the release trigger. scripts/bump-version rewrites both, and
    this is the guard for the day someone edits one by hand: the mirror is only safe
    because drifting fails CI.
    """
    import importlib.metadata

    import pytest_parallex

    assert pytest_parallex.__version__ == importlib.metadata.version("pytest-parallex")


class TestOptions:
    def test_parallel_option_recognized(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        assert result.ret == 0

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "2.5"])
    def test_bad_worker_count_is_a_usage_error(self, pytester, bad):
        """A typo in a flag must produce a usage error, not a traceback.

        Anything other than a UsageError raised from pytest_configure surfaces as an
        INTERNALERROR with a dump through pytest's internals: `--parallel-workers=abc`
        used to end in `ValueError: invalid literal for int()`.
        """
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", f"--parallel-workers={bad}")
        assert result.ret != 0
        result.stderr.fnmatch_lines(["*--parallel-workers must be*"])
        assert "INTERNALERROR" not in result.stdout.str()

    def test_parallel_process_option(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=process", "--parallel-workers=1")
        assert result.ret == 0

    def test_parallel_async_option(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=async", "--parallel-workers=1")
        assert result.ret == 0

    def test_invalid_mode_fails(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=invalid")
        assert result.ret != 0

    def test_no_parallel_runs_normally(self, pytester):
        pytester.makepyfile("def test_a(): assert True")
        result = pytester.runpytest()
        result.stdout.fnmatch_lines(["*1 passed*"])

    def test_workers_must_be_positive(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=0")
        assert result.ret != 0

    def test_collectonly_skips_runner(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--collect-only")
        result.stdout.fnmatch_lines(["*test_a*"])
        assert result.ret == 0
