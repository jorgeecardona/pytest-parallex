"""Hook specifications for pytest-parallex."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.hookspec(firstresult=True)
def pytest_parallex_setup(config: pytest.Config) -> dict[str, Any] | None:
    """Called once in the controller, before any worker starts.

    Return serializable data. Tests read it through the ``parallex_setup_data`` fixture
    in every worker.
    """


@pytest.hookspec
def pytest_parallex_teardown(config: pytest.Config, data: dict[str, Any]) -> None:
    """Called once after all parallel workers finish.

    Use this to clean up resources created in ``pytest_parallex_setup``.
    """


@pytest.hookspec(firstresult=True)
def pytest_parallex_auto_num_workers(config: pytest.Config) -> int | None:
    """Return the number of workers for ``--parallel-workers=auto``.

    The hook has no default implementation. When no plugin returns a value, the plugin
    uses ``os.cpu_count()``.
    """
