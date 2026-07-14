"""Test distribution strategies."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def schedule_round_robin(items: list[T], num_workers: int) -> list[list[T]]:
    """Distribute items across workers using round-robin.

    Each worker gets roughly ``len(items) / num_workers`` items.
    Items maintain relative order within each chunk.
    """
    if not items or num_workers < 1:
        return []

    actual_workers = min(num_workers, len(items))
    chunks: list[list[T]] = [[] for _ in range(actual_workers)]
    for i, item in enumerate(items):
        chunks[i % actual_workers].append(item)
    return chunks


def group_by_scope(items: list) -> list[list]:
    """Group items by module, largest group first.

    Keeps a module's tests together so module-scoped fixtures are set up once per
    worker that touches it, and puts the expensive groups at the head of the queue.
    Longest-processing-time-first is what makes a greedy queue near-optimal: claim
    the 3-minute module while there is still tail to absorb the imbalance, never last.
    """
    if not items:
        return []

    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(str(item.path), []).append(item)
    return sorted(groups.values(), key=len, reverse=True)


def schedule_by_scope(
    items: list,  # list[pytest.Item] but avoiding import
    num_workers: int,
) -> list[list]:
    """STATIC module-grouped partition — deal every group up front, then run.

    Only for the thread/async runners, which share one address space and so cannot
    pull from a cross-process queue. Process-based runners should use
    ``group_by_scope`` behind a claim counter instead.

    A static deal has to guess each group's cost from its test *count*, and count is
    a poor proxy for time. Measured on a real 1490-test suite whose per-module cost
    spanned 0.1s to 167s, this deal left the slowest worker holding 239s against a
    greedy queue's 167s — a 1.43x penalty, with 1832 core-seconds of workers idle.
    """
    if not items or num_workers < 1:
        return []

    sorted_groups = group_by_scope(items)
    actual_workers = min(num_workers, len(sorted_groups))
    chunks: list[list] = [[] for _ in range(actual_workers)]
    for i, group in enumerate(sorted_groups):
        chunks[i % actual_workers].extend(group)

    return chunks
