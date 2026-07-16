"""Shared preamble for a ``pytest_runtestloop`` implementation.

Every runner replaces pytest's test loop wholesale, which means every runner also inherits
the responsibilities that loop had. This is the one that is easy to forget and invisible
when forgotten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pytest_parallex import _worker_id_local

if TYPE_CHECKING:
    import pytest


def run_chunk(
    items: list[Any],
    session: pytest.Session,
    errors: list[BaseException],
    worker_id: str,
) -> None:
    """Run one worker's share of the items, in order, on the calling thread.

    Shared by the thread and async runners, which differ only in how they get a thread.

    ``nextitem`` is what makes this a *chunk* rather than a list of items, and it must
    come from within the chunk. pytest uses it to decide which scopes to tear down after
    an item: equal module means keep the module fixture alive, and so on. Pointing it at
    the globally-next item — which some other worker owns and may be running right now —
    tears down fixtures out from under that worker. It surfaces as pytest's own
    ``AssertionError: previous item was not torn down properly``, thrown from a thread
    that did nothing wrong.

    The last item's ``nextitem`` is the ``session``, not ``None``. ``None`` means "nothing
    follows, unwind everything", and every worker reaching its own end would tear the
    session down while its siblings are still running. Naming the session says "the next
    thing shares session scope", which stops the unwind one level short — the runner
    tears the session down itself, once, after every worker has joined.
    """
    _worker_id_local.id = worker_id
    try:
        for i, item in enumerate(items):
            nextitem = items[i + 1] if i + 1 < len(items) else session
            item.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
            if session.shouldfail or session.shouldstop:
                break
    except BaseException as exc:
        errors.append(exc)


def raise_if_collection_failed(session: pytest.Session) -> None:
    """Refuse to run when collection errored, exactly as pytest's own loop does.

    Without this, a suite with a broken import runs whatever *did* collect and reports
    success on it — a green run that silently skipped the module that failed to load.
    pytest's builtin ``pytest_runtestloop`` opens with this check; a plugin that takes
    over the loop takes over the check with it.

    The message is worded identically to pytest's so output doesn't change with the mode.
    """
    if session.testsfailed and not session.config.option.continue_on_collection_errors:
        errors = session.testsfailed
        raise session.Interrupted(f"{errors} error{'s' if errors != 1 else ''} during collection")
