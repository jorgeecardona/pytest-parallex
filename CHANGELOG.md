# Changelog

All notable changes to this project are documented here.

The version in `pyproject.toml` is the single source of truth, and merging a version bump
to `main` publishes it — see "Releasing" in the README.

## 0.1.1 — first release

The first published version is 0.1.1, not 0.1.0: `version-guard` requires a code change to
carry a version bump, and 0.1.0 never left this repo. Nothing was released as 0.1.0.

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

## 0.1.10

Driven by the first real-world macOS run and a coverage-gated CI adoption.

- **End-of-run summary line** — `pytest-parallex fork: 4 worker(s), 12 claim(s) |
  session fixtures: 3 hoisted, 1 per-worker`. "N passed" is the same string whether the
  plugin parallelised, degraded, or did nothing; this line is what tells those apart, and
  a failed hoist now says `FAILED to hoist` right in the summary.
- **macOS: the `_scproxy` fork segfault is disarmed in workers** (urllib proxy lookup
  rebound to the environment path; `no_proxy='*'` set when nothing proxy-ish exists).
  Found on a real suite: botocore's proxy detection segfaulted every worker, and
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY` did not help — different trap. The darwin warning
  now explains both traps.
- **Coverage under fork works** with `patch = ["fork"]` + `parallel = true` +
  `COVERAGE_PROCESS_START`: workers save their data explicitly before `os._exit` (which
  otherwise skips coverage's exit hook — measured: a 70%-gated suite read 39.8%).
- A session fixture that fails to hoist no longer poisons workers with the controller's
  cached exception; the worker builds it fresh and a real failure is attributed to the
  test that wanted it.

## 0.1.11

- **macOS `_scproxy` guard fixed at the right layer.** 0.1.10 rebound the *callers*
  (`urllib.request.proxy_bypass`/`getproxies`) — which misses botocore's import-time
  `from urllib.request import proxy_bypass` (a captured reference), and its `no_proxy`
  fallback was wiped by tests that clear `os.environ`. Verified ineffective on the same
  Mac that found the bug. Now the `*_macosx_sysconf` *leaves* are stubbed, which the
  original functions resolve at call time — catches every caller regardless of when they
  imported or what the environment says. Verified: 0 dead workers, 530/530 claims, and
  fork became the fastest configuration on that machine (30.0s vs xdist 38.2s).
