"""pytest-parallex: parallel test execution for pytest."""

from __future__ import annotations

import threading

# The single source of the version. hatchling reads this literal ([tool.hatch.version] in
# pyproject.toml), and scripts/bump-version rewrites it.
__version__ = "0.2.8"

_worker_id_local = threading.local()


def get_worker_id() -> str:
    """Return the id of the worker thread running this code, or ``'main'``.

    Set only in ``thread`` and ``async`` modes. In ``fork`` and ``process`` workers it
    returns ``'main'``: use the ``worker_id`` fixture there, which works in every mode.
    """
    return getattr(_worker_id_local, "id", "main")


def is_parallel_worker() -> bool:
    """Return True inside a ``thread`` or ``async`` mode worker.

    Returns False in ``fork`` and ``process`` workers, because ``get_worker_id`` returns
    ``'main'`` there. Compare the ``worker_id`` fixture to ``'main'`` to cover every mode.
    """
    return get_worker_id() != "main"
