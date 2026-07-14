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


def schedule_by_scope(
    items: list,  # list[pytest.Item] but avoiding import
    num_workers: int,
) -> list[list]:
    """Group items by module, then distribute groups round-robin.

    Keeps tests from the same module together so module-scoped fixtures
    only run once per worker.
    """
    if not items or num_workers < 1:
        return []

    # Group by module path
    groups: dict[str, list] = {}
    for item in items:
        key = str(item.path)
        groups.setdefault(key, []).append(item)

    # Sort groups by size (largest first) for better load balancing
    sorted_groups = sorted(groups.values(), key=len, reverse=True)

    actual_workers = min(num_workers, len(sorted_groups))
    chunks: list[list] = [[] for _ in range(actual_workers)]
    for i, group in enumerate(sorted_groups):
        chunks[i % actual_workers].extend(group)

    return chunks
