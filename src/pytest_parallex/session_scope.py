"""Make ``scope="session"`` mean *session* — once, not once per worker.

Under pytest-xdist, workers are spawned before collection and each one runs its own full
session, so a ``scope="session"`` fixture executes **once per worker**. Their docs say so
outright ("tests in different processes requesting a high-level scoped fixture ... will
execute the fixture code more than once") and ship a ``FileLock`` + shared-tmpdir recipe as
the workaround. It isn't a bug in anyone's test suite: their architecture has no moment at
which the controller could run a fixture once and hand the result to everybody.

Forking has that moment. If the controller sets a session fixture up *before* the fork, the
``FixtureDef`` — including its ``cached_result`` — is inherited by every child through
copy-on-write. Children requesting it get the cache and never re-run setup. One
testcontainer, one compiled asset, one login, shared by every worker, with no lockfile.

That leaves the scope pytest never had a name for: **the worker**. What xdist calls
"session" is really per-worker, and conflating the two is what forces the recipe. Here they
are separate — ``session`` runs in the controller, ``worker`` (see the ``worker_id``
fixture) runs once per forked child, and everything below is per-worker as pytest already
means it.

The constraint the physics imposes: whatever the controller builds is inherited, so it must
survive a fork. An address (a URL, a path, a port) survives. A live connection, a thread, or
an event loop does not — see ``_safety.assert_forkable``. The natural split is that the
controller owns the *server* and each worker opens its own *connection* to it.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Generator

    import pytest


@dataclass(frozen=True)
class EagerSetupResult:
    """What the controller managed to set up before forking.

    ``fixturedefs`` is kept in setup order so teardown can run in reverse — a fixture that
    depends on another must be finalized first.
    """

    names: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]  # (fixture name, error repr)
    fixturedefs: tuple[object, ...] = ()
    request: object = None  # the request the setups ran through; needed to finalize them

    @property
    def ok(self) -> bool:
        return not self.failures


#: A fixture that (transitively) wants one of these is per-WORKER, not per-session, by
#: definition — its whole purpose is to differ between workers. See is_worker_scoped.
#:
#: Both names, because the plugin answers to both: ``worker_id`` is the name existing
#: suites already write (and the reason a name, rather than a decorator, is the marker),
#: while ``parallex_worker_id`` is the alias for suites that would rather not rely on
#: winning a fixture-name race with pytest-xdist. Marking on only one would make the
#: alias a quiet trap: a ``database_url(parallex_worker_id)`` would look per-worker,
#: be hoisted anyway, and hand every worker the same database.
WORKER_MARKERS = frozenset({"worker_id", "parallex_worker_id"})

# Depth cap for walking fixture dependencies; pytest rejects real cycles at collection.
_MAX_CLOSURE_DEPTH = 10


def is_worker_scoped(name: str, item: pytest.Item, _depth: int = 0) -> bool:
    """True if this fixture depends on ``worker_id``, directly or through another fixture.

    This is the line between the two scopes xdist conflates. ``worker_id`` exists precisely
    to differ per worker, so anything downstream of it *cannot* be hoisted into the
    controller — one shared value is the one thing it must not be. Everything else that
    says ``scope="session"`` means it.

    Requiring ``worker_id`` is a better marker than a decorator because suites already
    write it that way. A real adopting suite's fixture is the archetype::

        @pytest.fixture(scope="session")
        def database_url(worker_id): ...   # carves app_test_gw0, _gw1, ... per worker

    It is declared session-scoped, but only because that is what xdist's session *means*.
    Hoisting it to the controller hands every worker one database, and a suite whose
    per-test fixture runs create_all/drop_all then wipes its siblings' schema mid-test —
    observed exactly that: `relation "users" does not exist`.
    """
    if name in WORKER_MARKERS:
        return True
    if _depth > _MAX_CLOSURE_DEPTH:  # a cycle would be a pytest bug, but don't hang on one
        return False
    defs = item.session._fixturemanager.getfixturedefs(name, item)
    if not defs:
        return False
    return any(is_worker_scoped(dep, item, _depth + 1) for dep in getattr(defs[-1], "argnames", ()))


def session_fixture_names(session: pytest.Session) -> tuple[list[str], list[str]]:
    """Session-scoped fixtures some collected test needs: (hoistable, per-worker).

    Walks each item's fixture closure rather than the whole registry: a suite can define
    session fixtures that nothing in this run requests, and setting those up eagerly would
    boot resources the run never uses — the opposite of the point.

    Fixtures downstream of ``worker_id`` land in the second list and are left to the
    workers (see ``is_worker_scoped``). They're returned rather than dropped so the
    end-of-run summary can say "2 per-worker" — a fixture silently *not* hoisted looks
    identical to a fixture hoisted, unless something reports the split.
    """
    hoistable: list[str] = []
    per_worker: list[str] = []
    seen: set[str] = set()

    for item in session.items:
        closure = getattr(item, "_fixtureinfo", None)
        if closure is None:
            continue
        for name in closure.names_closure:
            if name in seen:
                continue
            seen.add(name)
            defs = item.session._fixturemanager.getfixturedefs(name, item)
            if not defs:
                continue
            if defs[-1].scope == "session":
                if is_worker_scoped(name, item):
                    # Don't count the marker fixtures themselves ('worker_id',
                    # 'parallex_worker_id') — "per-worker" in the summary should mean the
                    # user's fixtures, and the markers are trivially per-worker noise.
                    if name not in WORKER_MARKERS:
                        per_worker.append(name)
                else:
                    hoistable.append(name)
    return hoistable, per_worker


def setup_session_fixtures(session: pytest.Session, names: list[str]) -> EagerSetupResult:
    """Set up the named session fixtures in the controller, caching them on their FixtureDefs.

    Uses a real item's request so the fixture resolves exactly as it would during a test —
    same conftest layering, same overrides, same parametrisation. The cache lands on the
    ``FixtureDef``, which every forked child then inherits.

    A fixture that raises here is *not* fatal: it is left uncached, so the worker that
    actually needs it will request it and fail there, attributed to the test that wanted it,
    with a normal traceback. Failing the whole run in the controller would report the error
    against nothing.
    """
    if not session.items:
        return EagerSetupResult(names=(), failures=())

    _activate_session_on_setup_stack(session)

    item = session.items[0]
    request = cast("pytest.FixtureRequest", item._request)  # type: ignore[attr-defined]
    done: list[str] = []
    failures: list[tuple[str, str]] = []

    with _borrowed_item_active(session, item):
        for name in names:
            try:
                request.getfixturevalue(name)
            except Exception as exc:  # noqa: PERF203 — one failed hoist must not abort its siblings
                failures.append((name, repr(exc)))
                # pytest caches a fixture-body exception on the FixtureDef, and children
                # inherit that cache — so without this, a failed hoist doesn't fall back
                # to per-worker setup, it poisons every worker with the controller's
                # exception. And execute() registers its post-finalizer *before* running
                # the body, so the failed attempt also leaves an entry in _finalizers that
                # trips pytest 9.1's `assert not self._finalizers` when the worker
                # retries. Clearing both makes the docstring true: the worker that needs
                # the fixture builds it fresh, and a genuinely broken fixture fails there,
                # attributed to the test that wanted it.
                defs = session._fixturemanager.getfixturedefs(name, item)
                if defs:
                    defs[-1].cached_result = None
                    defs[-1]._finalizers.clear()
            else:
                done.append(name)

    return EagerSetupResult(names=tuple(done), failures=tuple(failures), request=request)


@contextmanager
def _borrowed_item_active(
    session: pytest.Session, item: pytest.Item
) -> Generator[None, None, None]:
    """Make the borrowed item look set-up for the duration of the hoist.

    ``getfixturevalue`` decides it is being called "during teardown" by asking
    ``is_node_active(request.node)``, which is just ``node in setupstate.stack``. We borrow
    ``session.items[0]``'s request and never run that item, so it is not on the stack, and
    pytest concluded we were tearing down::

        PytestRemovedIn10Warning: Calling request.getfixturevalue("postgres") during
        teardown is deprecated.

    That is not cosmetic. The warning goes to ``setup_session_fixtures``' own except
    branch under ``filterwarnings = error`` — a common setting — so every hoist "failed",
    every fixture was left to the workers, and the whole feature silently degraded to
    xdist's once-per-worker with a green run and nothing in the summary. pytest's own TODO
    says 10.1 raises unconditionally, which would do the same for everyone.

    Pushing the item makes the check true. It has to come back off again: ``SetupState``
    asserts every stack entry is in the *next* item's parent chain, so an item left behind
    makes the first worker to run a different one die with "previous item was not torn down
    properly".

    Empty finalizer list, because this item is a stand-in and never really ran — there is
    nothing of its own to tear down. A session fixture's own finalizers do not land here
    anyway: they attach to ``subrequest.node``, which for session scope is the Session.
    """
    stack = session._setupstate.stack  # type: ignore[attr-defined]
    borrowed = item not in stack
    if borrowed:
        stack[item] = ([], None)
    try:
        yield
    finally:
        if borrowed:
            stack.pop(item, None)


def _activate_session_on_setup_stack(session: pytest.Session) -> None:
    """Put the Session node on pytest's setup stack so finalizers can attach to it.

    ``SetupState.addfinalizer`` asserts ``node in self.stack``, and the stack is only
    populated by ``SetupState.setup(item)`` — which the controller never calls, because it
    runs no tests. Without this, a session fixture *executes* and caches, but its teardown
    silently never registers: you get the resource and no way to release it. (Observed
    exactly that: one Postgres booted, zero torn down, on every run.)

    ``item.listchain()[0]`` is always the Session, and this is the same entry
    ``SetupState.setup`` would push for it — the node paired with its own ``teardown`` and
    no exception.
    """
    stack = session._setupstate.stack  # type: ignore[attr-defined]
    if session not in stack:
        stack[session] = ([session.teardown], None)


def detach_in_child(session: pytest.Session) -> None:
    """Disown the controller's session-fixture finalizers. Call once, in each forked child.

    A child inherits the whole setup stack, finalizers included — so when it finishes its
    last test and pytest unwinds with ``teardown_exact(None)``, it happily tears down the
    session fixtures the *controller* owns. Two things then go wrong, and both were
    observed:

    * The child stops a server its siblings are still using.
    * Teardown clears ``cached_result``, so the child's *next* group re-runs setup — one
      boot per group instead of one per run, which is worse than the xdist behaviour this
      feature exists to fix.

    Dropping the finalizers (while leaving the Session on the stack, so ``addfinalizer``
    still works for anything the child sets up itself) makes the ownership explicit: the
    controller set them up, the controller tears them down, children only read the cache.
    """
    stack = session._setupstate.stack  # type: ignore[attr-defined]
    if session in stack:
        stack[session] = ([], None)


def teardown_session_fixtures(session: pytest.Session) -> list[tuple[str, str]]:
    """Unwind the controller's setup stack, finalizing session fixtures. Returns errors.

    Delegates to pytest's own ``teardown_exact`` rather than calling ``FixtureDef.finish``
    by hand, so finalizers run in the order pytest guarantees and the
    ``pytest_fixture_post_finalizer`` hook still fires for plugins that watch it.
    """
    try:
        session._setupstate.teardown_exact(None)  # type: ignore[attr-defined]
    except Exception as exc:  # deliberately broad — a leaked resource must be reported, not raised
        return [("session", repr(exc))]
    return []
