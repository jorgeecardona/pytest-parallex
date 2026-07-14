"""Integration tests for the async runner."""

from __future__ import annotations


class TestAsyncBasic:
    def test_simple_pass(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert True

            def test_b():
                assert True
        """)
        result = pytester.runpytest("--parallel=async", "--parallel-workers=2", "-v")
        result.stdout.fnmatch_lines(["*2 passed*"])
        assert result.ret == 0

    def test_simple_fail(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert False
        """)
        result = pytester.runpytest("--parallel=async", "--parallel-workers=1")
        result.stdout.fnmatch_lines(["*1 failed*"])
        assert result.ret == 1

    def test_empty_suite(self, pytester):
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=async", "--parallel-workers=2")
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_session_fixture_runs_once(self, pytester):
        pytester.makepyfile("""
            import pytest

            call_count = 0

            @pytest.fixture(scope="session")
            def shared():
                global call_count
                call_count += 1
                return call_count

            def test_a(shared):
                assert shared == 1

            def test_b(shared):
                assert shared == 1
        """)
        result = pytester.runpytest("--parallel=async", "--parallel-workers=2", "-v")
        result.stdout.fnmatch_lines(["*2 passed*"])
