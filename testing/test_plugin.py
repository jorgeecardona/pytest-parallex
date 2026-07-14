"""Tests for plugin CLI options and configuration."""

from __future__ import annotations


class TestOptions:
    def test_parallel_option_recognized(self, pytester):
        pytester.makepyfile("def test_a(): pass")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        assert result.ret == 0

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
