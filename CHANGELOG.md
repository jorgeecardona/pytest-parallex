# Changelog

All notable changes to this project are documented here.

The version in `pyproject.toml` is the single source of truth, and merging a version bump
to `main` publishes it — see "Releasing" in the README.

## 0.1.0 — unreleased

First release.

### Added

- `--parallel=fork`: collect once in the controller, then fork warm workers that inherit
  the imported suite and the collected items via copy-on-write.
- **Controller-side session fixtures.** `scope="session"` runs once, in the controller,
  before the fork; children inherit the cached result. Under pytest-xdist the same fixture
  runs once *per worker*. `--parallex-no-session-scope` restores the per-worker behaviour.
- `--parallel=thread`, `--parallel=async`, `--parallel=process`.
- `--parallel-workers=N` (default `auto` = CPU count).
- Fork-safety refusal: the run stops with a message naming the offending thread or event
  loop rather than forking a process that would deadlock in the child.
- Logging `QueueListener` threads are stopped and restarted around the fork, so a suite
  that merely imports litestar (or anything using `QueueHandler`) can still fork.
- Hooks: `pytest_parallex_setup`, `pytest_parallex_teardown`,
  `pytest_parallex_auto_num_workers`.
- Fixtures: `parallex_worker_id`, `parallex_mode`, `parallex_setup_data`.
- Dynamic work-claiming: workers race for module groups off a shared counter rather than
  being dealt a static partition, so a slow module can't strand a worker.

### Requires

- **pytest >= 8.1**, not 8.0: `FixtureManager.getfixturedefs` took a nodeid string until
  8.1 and a `Node` from 8.1 on, and the fork runner passes a `Node`.
- Tested against pytest 8.1, 8.4, 9.0 and 9.1 across all four modes. CI pins the floor.

### Worker-scoped fixtures

- A `scope="session"` fixture that requests `worker_id` (directly or transitively) is
  per-worker and is never hoisted to the controller — so `postgres_server` (one container)
  and `database_url(worker_id)` (one database per worker) can coexist, which is what
  xdist's filelock recipe exists to fake.
- `worker_id` is provided by parallex when `--parallel` is active, registered late enough
  to win the name from pytest-xdist. A plain `pytest -n 4` run keeps xdist's.
