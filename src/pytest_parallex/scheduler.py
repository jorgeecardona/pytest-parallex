"""Test distribution strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    import pytest

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


def group_by_scope(items: list[pytest.Item]) -> list[list[pytest.Item]]:
    """Group items by module, largest group first.

    Keeps a module's tests together so module-scoped fixtures are set up once per
    worker that touches it, and puts the expensive groups at the head of the queue.
    Longest-processing-time-first is what makes a greedy queue near-optimal: claim
    the 3-minute module while there is still tail to absorb the imbalance, never last.
    """
    if not items:
        return []

    groups: dict[str, list[pytest.Item]] = {}
    for item in items:
        groups.setdefault(str(item.path), []).append(item)
    return sorted(groups.values(), key=len, reverse=True)


_COARSE_SCOPES = frozenset({"package", "module", "class"})


def _keeps_module_together(item: pytest.Item) -> bool:
    """True if this test needs a fixture set up per-module (or coarser) in the worker.

    A module's tests must stay on one worker only to keep such a fixture set up once for
    the module. A test that uses only ``function`` and ``session`` fixtures needs nothing
    kept alive across tests — session fixtures are built once in the controller and
    inherited by every worker — so it is free to run anywhere.

    ``worker_id``-scoped fixtures don't count: those are per-worker by design, so each
    worker rebuilds its own no matter how the tests are dealt, and pinning the module buys
    nothing.
    """
    from pytest_parallex.session_scope import is_worker_scoped

    closure = getattr(item, "_fixtureinfo", None)
    if closure is None:
        return True  # unknown shape — be safe, keep it grouped
    fm = item.session._fixturemanager  # type: ignore[attr-defined]
    for name in closure.names_closure:
        defs = fm.getfixturedefs(name, item)
        if defs and defs[-1].scope in _COARSE_SCOPES and not is_worker_scoped(name, item):
            return True
    return False


def plan_fork_claims(items: list[pytest.Item]) -> list[list[pytest.Item]]:
    """The unit of work each forked worker claims off the shared counter.

    Extends ``group_by_scope`` with one move that removes fork's worst case. A module was
    the atomic claim, so parallelism was capped at ``min(workers, modules)`` — one big
    file of function-fixture tests ran on a single worker, i.e. serially, while xdist
    spread the same tests across every core.

    Here a module that keeps no coarse fixture alive (only ``function`` and hoisted
    ``session`` fixtures) is dealt as *individual tests* any worker can claim, so those
    fill the machine. A module that does own a package/module/class fixture stays whole,
    so that fixture is still set up exactly once for it — the property the grouping exists
    to protect, unchanged.

    Order: heaviest claims first (the whole-module groups, longest-processing-time-first),
    then the singletons. A greedy queue wants the big indivisible chunk claimed while there
    is still small work left to balance against it.
    """
    if not items:
        return []

    modules: dict[str, list[pytest.Item]] = {}
    for item in items:
        modules.setdefault(str(item.path), []).append(item)

    grouped: list[list[pytest.Item]] = []
    singletons: list[list[pytest.Item]] = []
    for group in modules.values():
        if any(_keeps_module_together(it) for it in group):
            grouped.append(group)
        else:
            singletons.extend([it] for it in group)

    grouped.sort(key=len, reverse=True)
    return grouped + singletons


def schedule_by_scope(
    items: list[pytest.Item],
    num_workers: int,
) -> list[list[pytest.Item]]:
    """STATIC module-grouped partition: deal every group up front, then run.

    Only for the thread/async runners, which share one address space and so cannot
    pull from a cross-process queue. Process-based runners should use
    ``group_by_scope`` behind a claim counter instead.

    A static deal has to guess each group's cost from its test *count*, and count is
    a poor proxy for time. Measured on a real 1490-test suite whose per-module cost
    spanned 0.1s to 167s, this deal left the slowest worker holding 239s against a
    greedy queue's 167s — a 1.43x penalty, with 1832 core-seconds of workers idle.

    Groups are dealt to the worker holding the least so far (longest-processing-time-first,
    the standard greedy approximation). Note that "least" is still measured in tests, so
    the guess above is what's being optimised. LPT bounds the damage at 4/3 of optimal
    *for the counts it was given*, which is only as good as count-as-proxy-for-time. Plain
    round-robin over the sorted groups has no such bound at all:
    with 4 workers it hands groups 0 and 4 to the same worker regardless of what the
    groups in between cost.
    """
    if not items or num_workers < 1:
        return []

    sorted_groups = group_by_scope(items)
    actual_workers = min(num_workers, len(sorted_groups))
    chunks: list[list[pytest.Item]] = [[] for _ in range(actual_workers)]

    for group in sorted_groups:
        lightest = min(range(actual_workers), key=lambda i: len(chunks[i]))
        chunks[lightest].extend(group)

    return chunks
