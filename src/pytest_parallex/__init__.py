"""pytest-parallex: Simple parallel test execution for pytest."""

from __future__ import annotations

import threading

# Kept in lockstep with pyproject.toml by scripts/bump-version; pyproject stays the
# release trigger (version-guard and the publish check read it). A test asserts the two
# never drift.
__version__ = "0.2.2"

_worker_id_local = threading.local()


def get_worker_id() -> str:
    """Return the current worker ID ('w0', 'w1', ...) or 'main'."""
    return getattr(_worker_id_local, "id", "main")


def is_parallel_worker() -> bool:
    """Return True if running inside a parallel worker."""
    return get_worker_id() != "main"
