"""Tests for plugin CLI options and configuration."""

from __future__ import annotations

import pytest


def test_dunder_version_matches_the_package_metadata() -> None:
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
    def test_parallel_option_recognized(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        assert result.ret == 0

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "2.5"])
    def test_bad_worker_count_is_a_usage_error(self, pytester: pytest.Pytester, bad: str) -> None:
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

    def test_parallel_process_option(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=process", "--parallel-workers=1")
        assert result.ret == 0

    def test_parallel_async_option(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=async", "--parallel-workers=1")
        assert result.ret == 0

    def test_invalid_mode_fails(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=invalid")
        assert result.ret != 0

    def test_no_parallel_runs_normally(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): assert True")
        result = pytester.runpytest()
        result.stdout.fnmatch_lines(["*1 passed*"])

    def test_workers_must_be_positive(self, pytester: pytest.Pytester) -> None:
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=0")
        assert result.ret != 0

    def test_collectonly_skips_runner(self, pytester: pytest.Pytester) -> None:
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

    def test_warns_on_darwin_without_escape_hatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        monkeypatch.delenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert any("OBJC_DISABLE_INITIALIZE_FORK_SAFETY" in str(w.message) for w in caught)

    def test_silent_when_escape_hatch_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        monkeypatch.setenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert not caught

    def test_silent_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "linux")
        monkeypatch.delenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", raising=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _safety._warn_darwin_fork_caveat()
        assert not caught

    def test_never_escalates_under_error_filters(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`filterwarnings = error` must not turn the caveat into an INTERNALERROR.

        The escalated warning surfaced mid-pytest_runtestloop and aborted the whole run
        on a real macOS suite with strict warnings; the advice belongs on stderr instead.
        """
        import warnings

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        monkeypatch.delenv("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", raising=False)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _safety._warn_darwin_fork_caveat()
        assert "OBJC_DISABLE_INITIALIZE_FORK_SAFETY" in capsys.readouterr().err


class TestDarwinScproxyGuard:
    """The _scproxy hardening — the crash a real Mac actually hit, fixed at the right layer.

    Workers SIGSEGV'd inside urllib's proxy_bypass_macosx_sysconf (SystemConfiguration via
    _scproxy, reached from botocore's proxy detection). Two earlier layers failed on the
    same hardware: rebinding the CALLERS missed botocore's import-time `from urllib.request
    import proxy_bypass` (a captured reference), and a no_proxy env fallback was wiped by
    `patch.dict(os.environ, clear=True)`. Patching the LEAVES works because the original
    proxy_bypass resolves them in urllib.request's globals at call time — verified on the
    Mac: 0 dead workers, 530/530 claims.

    These tests fake the platform both ways, so they pin the same behavior on any host:
    the stubs are installed on (faked) darwin, behave, and a non-darwin platform is left
    untouched. urllib is restored exactly afterwards — a linux host never had the leaves,
    a darwin host keeps its real ones.
    """

    _LEAF_NAMES = ("proxy_bypass_macosx_sysconf", "getproxies_macosx_sysconf")
    _MISSING = object()

    def _snapshot_leaves(self) -> dict[str, object]:
        import urllib.request

        return {name: getattr(urllib.request, name, self._MISSING) for name in self._LEAF_NAMES}

    def _restore_leaves(self, snapshot: dict[str, object]) -> None:
        import urllib.request

        for name, original in snapshot.items():
            if original is self._MISSING:
                if hasattr(urllib.request, name):
                    delattr(urllib.request, name)
            else:
                setattr(urllib.request, name, original)

    def test_installs_leaf_stubs_on_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import urllib.request

        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "darwin")
        snapshot = self._snapshot_leaves()
        _safety.harden_darwin_fork_child()
        try:
            assert urllib.request.proxy_bypass_macosx_sysconf("anything") is False  # type: ignore[attr-defined]
            assert urllib.request.getproxies_macosx_sysconf() == {}  # type: ignore[attr-defined]
        finally:
            self._restore_leaves(snapshot)
            _safety.uninstall_forksafe_getaddrinfo()

    def test_noop_off_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pytest_parallex import _safety

        monkeypatch.setattr(_safety.sys, "platform", "linux")
        before = self._snapshot_leaves()
        _safety.harden_darwin_fork_child()
        assert self._snapshot_leaves() == before
