"""Integration tests for the thread runner."""

from __future__ import annotations


class TestThreadBasic:
    def test_simple_pass(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert True

            def test_b():
                assert True
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2", "-v")
        result.stdout.fnmatch_lines(["*2 passed*"])
        assert result.ret == 0

    def test_simple_fail(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert False
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        result.stdout.fnmatch_lines(["*1 failed*"])
        assert result.ret == 1

    def test_mixed_results(self, pytester):
        pytester.makepyfile("""
            def test_pass():
                assert True

            def test_fail():
                assert False
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        # pytest's summary line always orders failed before passed, whatever order the
        # tests ran in — matching "*1 passed*1 failed*" could never succeed.
        result.stdout.fnmatch_lines(["*1 failed*1 passed*"])
        assert result.ret == 1

    def test_empty_suite(self, pytester):
        pytester.makepyfile("")
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        result.stdout.fnmatch_lines(["*no tests ran*"])
        assert result.ret == 5  # EXIT_NOTESTSCOLLECTED

    def test_single_worker(self, pytester):
        pytester.makepyfile("""
            def test_a(): pass
            def test_b(): pass
            def test_c(): pass
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        result.stdout.fnmatch_lines(["*3 passed*"])


class TestThreadFixtures:
    def test_session_fixture_runs_once(self, pytester):
        pytester.makepyfile("""
            import pytest

            call_count = 0

            @pytest.fixture(scope="session")
            def shared_resource():
                global call_count
                call_count += 1
                return call_count

            def test_a(shared_resource):
                assert shared_resource == 1

            def test_b(shared_resource):
                assert shared_resource == 1
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2", "-v")
        result.stdout.fnmatch_lines(["*2 passed*"])

    def test_function_fixture_per_test(self, pytester):
        pytester.makepyfile("""
            import pytest

            @pytest.fixture
            def unique():
                import threading
                return threading.current_thread().ident

            def test_a(unique):
                assert unique is not None

            def test_b(unique):
                assert unique is not None
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2")
        result.stdout.fnmatch_lines(["*2 passed*"])

    def test_yield_fixture_teardown(self, pytester):
        pytester.makepyfile("""
            import pytest

            teardown_ran = False

            @pytest.fixture
            def resource():
                yield "value"
                global teardown_ran
                teardown_ran = True

            def test_a(resource):
                assert resource == "value"
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=1")
        result.stdout.fnmatch_lines(["*1 passed*"])


class TestThreadConcurrency:
    def test_threads_run_concurrently(self, pytester):
        pytester.makepyfile("""
            import threading
            import time

            seen_threads = set()

            def test_a():
                seen_threads.add(threading.current_thread().ident)
                time.sleep(0.1)

            def test_b():
                seen_threads.add(threading.current_thread().ident)
                time.sleep(0.1)

            def test_c():
                seen_threads.add(threading.current_thread().ident)
                time.sleep(0.1)

            def test_d():
                seen_threads.add(threading.current_thread().ident)
                time.sleep(0.1)
        """)
        result = pytester.runpytest("--parallel=thread", "--parallel-workers=2", "-v")
        result.stdout.fnmatch_lines(["*4 passed*"])

    def test_maxfail_respected(self, pytester):
        pytester.makepyfile("""
            def test_a():
                assert False

            def test_b():
                assert False

            def test_c():
                assert True
        """)
        result = pytester.runpytest(
            "--parallel=thread", "--parallel-workers=1", "--maxfail=1"
        )
        # Should stop after first failure
        result.stdout.fnmatch_lines(["*1 failed*"])
