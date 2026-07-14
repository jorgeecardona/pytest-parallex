"""Integration tests for the process runner."""

from __future__ import annotations


class TestProcessBasic:
    def test_simple_pass(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert True

            def test_b():
                assert True
        """)
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        assert result.ret == 0

    def test_simple_fail(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert False
        """)
        result = pytester.runpytest("--parallel=process", "--parallel-workers=1")
        assert result.ret != 0

    def test_empty_suite(self, pytester):
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_multiple_files(self, pytester):
        pytester.makepyfile(
            test_one="def test_a(): pass",
            test_two="def test_b(): pass",
        )
        result = pytester.runpytest("--parallel=process", "--parallel-workers=2")
        assert result.ret == 0
