"""pytest-parallex: Simple parallel test execution for pytest."""

from __future__ import annotations

import threading

_worker_id_local = threading.local()


def get_worker_id() -> str:
    """Return the current worker ID ('w0', 'w1', ...) or 'main'."""
    return getattr(_worker_id_local, "id", "main")


def is_parallel_worker() -> bool:
    """Return True if running inside a parallel worker."""
    return get_worker_id() != "main"
