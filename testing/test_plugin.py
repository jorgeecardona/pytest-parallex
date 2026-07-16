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


class TestDarwinForkCaveat:
    """The macOS ObjC fork trap warning — testable anywhere by faking the platform.

    The real failure was found by Jorge on an actual Mac: a "Python quit unexpectedly"
    dialog per worker and nothing in the summary, because the ObjC runtime kills forked
    children that touch a pre-fork-initialized Apple framework. The plugin can't fix it
    (OBJC_DISABLE_INITIALIZE_FORK_SAFETY must be set before Python starts), so the one
    thing it owes the user is a warning BEFORE the crashes that names the fix.
    """

    def test_warns_on_darwin_without_escape_hatch(self, monkeypatch):
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        monkeypatch.delenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert any("OBJC_DISABLE_INITIALIZE_FORK_SAFETY" in str(w.message) for w in caught)

    def test_silent_when_escape_hatch_is_set(self, monkeypatch):
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        monkeypatch.setenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert not caught

    def test_silent_on_linux(self):
        import warnings

        from pytest_parallex import _safety

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert not caught
