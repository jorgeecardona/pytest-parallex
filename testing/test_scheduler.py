"""Tests for test distribution strategies."""

from __future__ import annotations

from pytest_parallex.scheduler import plan_fork_claims, schedule_round_robin


class TestPlanForkClaims:
    """``plan_fork_claims`` needs live pytest items to inspect fixture closures, so its
    behaviour is covered end-to-end in test_regressions.py::TestForkFillsWorkers. These
    pin the parts that are pure list-shaping and need no fixtures.
    """

    def test_empty(self):
        assert plan_fork_claims([]) == []


class TestRoundRobin:
    def test_basic_distribution(self):
        result = schedule_round_robin([1, 2, 3, 4], 2)
        assert result == [[1, 3], [2, 4]]

    def test_single_worker(self):
        result = schedule_round_robin([1, 2, 3], 1)
        assert result == [[1, 2, 3]]

    def test_more_workers_than_items(self):
        result = schedule_round_robin([1, 2], 5)
        assert len(result) == 2
        assert result == [[1], [2]]

    def test_empty_items(self):
        assert schedule_round_robin([], 3) == []

    def test_zero_workers(self):
        assert schedule_round_robin([1, 2], 0) == []

    def test_preserves_order_within_chunk(self):
        result = schedule_round_robin([1, 2, 3, 4, 5, 6], 2)
        assert result[0] == [1, 3, 5]
        assert result[1] == [2, 4, 6]

    def test_single_item(self):
        result = schedule_round_robin(["a"], 3)
        assert result == [["a"]]

    def test_equal_distribution(self):
        result = schedule_round_robin(list(range(9)), 3)
        assert all(len(chunk) == 3 for chunk in result)
