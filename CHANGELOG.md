# Changelog

All notable changes to pytest-parallex are documented here. Versioning is
[SemVer](https://semver.org/); the version is `__version__` in
`src/pytest_parallex/__init__.py` (hatchling reads it) and is bumped automatically when
shipped source changes. Merging a version bump to `main` publishes it (see
[Releasing](https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/CONTRIBUTING.md#releasing)).

## [Unreleased]

## [0.2.7] - 2026-11-21

### Changed

- README and PyPI page: new header with the docs links, an Overview that states the platform
  limits (fork mode is for Linux, macOS has caveats, Windows is not tested in CI), and a new
  PyPI summary.
- The benchmarks page starts with the current result against pytest-xdist. Older measurements
  are marked as superseded.

### Fixed

- Docstrings: the fixture for `pytest_parallex_setup` data is `parallex_setup_data`;
  `get_worker_id()` and `is_parallel_worker()` report a worker only in `thread` and `async`
  modes (use the `worker_id` fixture in `fork` and `process` modes).
- README: the pytest floor is 8.1 because of a change in pytest 8.1, not 8.0.

## [0.2.6] - 2026-11-14

### Changed

- Clearer PyPI summary and README opening, with working LICENSE links on PyPI.

## [0.2.5] - 2026-10-19

### Changed

- The project links on PyPI (Homepage, Repository, Issues, Changelog, Documentation) point to
  the `cardona-oss` GitLab group. No code change.

## [0.2.4] - 2026-09-25

### Fixed

- **Fork mode: a failing comparison in a worker prints its message and pytest's diff again**
  ([#2](https://gitlab.com/cardona-oss/pytest-parallex/-/issues/2),
  [#4](https://gitlab.com/cardona-oss/pytest-parallex/-/issues/4)). The worker
  unregistered the `terminalreporter` plugin to keep from writing progress to the shared
  stdout, but pytest's `assertrepr_compare` fetches its syntax highlighter through
  `config.get_terminal_writer()`, which asserts that plugin is registered. Every failing
  comparison in a worker therefore surfaced as a bare `AssertionError` from
  `_pytest/config/__init__.py` with the left and right values hidden. The worker now keeps
  the reporter registered and redirects its writer to `/dev/null` instead: nothing reaches
  the terminal from the child, and the diff comes back.

## [0.2.3] - 2026-08-26

### Fixed

- **Fork mode: private first-import cache dirs per worker**
  ([#1](https://gitlab.com/cardona-oss/pytest-parallex/-/issues/1)). matplotlib, numba and
  pytensor build a cache on the first import in a process; N forked workers importing into
  one cold shared dir raced on half-written files and could crash. Each worker now gets its
  own `MPLCONFIGDIR`, `NUMBA_CACHE_DIR` and pytensor `base_compiledir`, and the controller
  removes them at the end. The README documents importing the library pre-fork (a hoisted
  session fixture) to build the cache once and inherit it.

## [0.2.2] - 2026-07-17

### Fixed

- **Process mode: coverage data is namespaced per worker and combined on the controller.**
  Every pytest-cov session ends by combining `$COVERAGE_FILE.*`, so N workers sharing the
  default name combined and deleted each other's data files mid-combine (sqlite3 "no such
  table", `INTERNALERROR` exit 3, that worker's remaining tests missing; timing-dependent,
  so it surfaced as flaky CI). Each worker now measures into `.coverage.wN` with
  `--cov-report=` and `--cov-fail-under=0` pinned, and the controller's session-end combine
  produces the one reported, gated total.

## [0.2.1] - 2026-07-16

### Fixed

- **Python 3.10 floor.** `urllib.request` was imported lazily inside
  `harden_darwin_fork_child`, so a first import under a faked `sys.platform="darwin"` ran
  3.10's `from _scproxy import ...` on Linux and died. It is now imported at module top under
  the real platform, with a fallback that masks the fake platform when `_scproxy` is missing.
- The floor task pinned `pytest==8.1.0`, which upstream yanked; moved to 8.1.1.
- The fork tests that fake `sys.platform="darwin"` and then fork into the macOS-only
  hardening are skipped off macOS: the Linux CI container segfaulted (exit 139) where
  every dev box passed. The hardening functions stay unit-tested everywhere without forking.

## [0.2.0] - 2026-07-16

A deliberate minor with no functional change from 0.1.13. The 0.1.x history (and the
published 0.1.11 artifacts) carried a private project name in docstrings; the history was
rewritten clean and 0.2.0 starts from it.

## [0.1.13] - 2026-07-16

### Changed

- ruff `select = ["ALL"]` with every opt-out justified inline, and pyright strict with
  `reportPrivateUsage` as the one documented exception. Annotates the test suite and the
  scheduler, makes `subprocess` `check=` explicit, drops unused hook params (pluggy passes
  by name), moves file ops to `pathlib`.
- Mac-only tests fork into the real `_scproxy` and libinfo crash paths and assert the child
  dies; paired survival tests assert `harden_darwin_fork_child` defuses both.

### Fixed

- `scripts/bump-version` left `<file>-E` backups on macOS: BSD sed read the `-E` after
  `-i` as a backup suffix. It now replaces through a temp file.

## [0.1.12] - 2026-07-16

Docstring polish on the macOS fork-trap code; no functional change.

## [0.1.11] - 2026-07-16

### Fixed

- **macOS `_scproxy` guard fixed at the right layer.** 0.1.10 rebound the callers
  (`urllib.request.proxy_bypass`/`getproxies`), which misses botocore's import-time
  `from urllib.request import proxy_bypass` (a captured reference), and its `no_proxy`
  fallback was wiped by tests that clear `os.environ`. Verified ineffective on the same
  Mac that found the bug. Now the `*_macosx_sysconf` leaves are stubbed, which the
  original functions resolve at call time, so every caller is covered regardless of when
  it imported or what the environment says. Verified: 0 dead workers, 530/530 claims, and
  fork became the fastest configuration on that machine (30.0s vs xdist 38.2s).
- **macOS: the second fork segfault, DNS resolution, is disarmed in workers.** Distinct
  from `_scproxy` and found on the same suite once that guard held: workers died in
  `socket.getaddrinfo` reached from httpx opening a cloud endpoint. Minimal reproduction:
  a child forked from a parent that has ever resolved a real hostname segfaults on its own
  first lookup (libinfo's mDNSResponder client state does not survive the fork), and the
  parent has almost always resolved one; pulling a container image is enough.
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY` does not cover it. A segfault cannot be caught, so
  workers now resolve real names in a short-lived `python -I -c` subprocess (fresh exec,
  pristine libinfo) with a per-endpoint cache; numeric addresses stay on the real
  `getaddrinfo` via an `AI_NUMERICHOST` first attempt. Verified on the suite that found
  it: 0 dead workers, 187/187 accounted for.

## [0.1.10] - 2026-07-16

Driven by the first real-world macOS run and a coverage-gated CI adoption.

### Added

- **End-of-run summary line**: `pytest-parallex fork: 4 worker(s), 12 claim(s) |
  session fixtures: 3 hoisted, 1 per-worker`. "N passed" is the same string whether the
  plugin parallelised, degraded, or did nothing; this line is what tells those apart, and
  a failed hoist now says `FAILED to hoist` right in the summary.
- **Coverage under fork works** with `patch = ["fork"]` + `parallel = true` +
  `COVERAGE_PROCESS_START`: workers save their data explicitly before `os._exit` (which
  otherwise skips coverage's exit hook; measured: a 70%-gated suite read 39.8%).

### Fixed

- **macOS: the `_scproxy` fork segfault is disarmed in workers** (urllib proxy lookup
  rebound to the environment path; `no_proxy='*'` set when nothing proxy-ish exists).
  Found on a real suite: botocore's proxy detection segfaulted every worker, and
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY` did not help (different trap). The darwin warning
  now explains both traps. Superseded by the 0.1.11 fix.
- A session fixture that fails to hoist no longer poisons workers with the controller's
  cached exception; the worker builds it fresh and a real failure is attributed to the
  test that wanted it.

## [0.1.9] - 2026-07-16

- Warn about the macOS ObjC `+initialize` fork trap and how to disarm it.
- The Python 3.10 floor from `requires-python` is now tested in CI (`test-pytest-floor`),
  not just asserted.

## [0.1.8] - 2026-07-16

- `pytest_parallex.__version__` mirrors the version in `pyproject.toml`.

## [0.1.7] - 2026-07-16

- `worker_id` is provided on plain serial runs too (`'main'`), so a conftest can request it
  without `--parallel`. With pytest-xdist installed and idle, xdist's own `'master'` wins.

## [0.1.6] - 2026-07-16

- **Fork mode fills every worker.** `plan_fork_claims` replaces `group_by_scope`: a module
  whose tests keep no package, module or class fixture alive is dealt test-by-test, so a
  suite with fewer modules than cores no longer caps parallelism at the module count. A
  module that owns such a fixture still stays on one worker, so the fixture is set up once
  for it.

## [0.1.5] - 2026-07-16

- **Thread and async modes: function-scoped fixtures were unsound.** `FixtureDef` keeps one
  `cached_result` and one `_finalizers` list shared across threads, so two workers running
  two tests shared that one slot and one thread's setup ran while another's teardown
  cleared it. Those two attributes are now per-thread for function scope; session and
  module scope stay shared, which is the feature.

## [0.1.4] - 2026-07-16

- **Session-fixture hoisting was silently inert under `filterwarnings = error`.** pytest 9
  warns `PytestRemovedIn10Warning` when `getfixturevalue` is called for a node that is not
  on the setup stack; with warnings as errors that warning became an exception in the hoist
  path, every fixture fell back to the workers, and the run stayed green. The borrowed item
  is now pushed onto the setup stack for the hoist. Regression test
  `TestHoistingUnderStrictWarnings` asserts the fixture boot count.

## [0.1.3] - 2026-07-15

- Benchmarks against xdist recorded in `docs/benchmarks.md`, including the measurements
  that looked convincing and were wrong.

## [0.1.2] - 2026-07-15

- Ship `py.typed`; gate formatting in CI; shorten the PyPI blurb so search results do not
  truncate it.

## [0.1.1] - 2026-07-15

First published version. 0.1.0 never left this repo: `version-guard` requires a code change
to carry a version bump, and the first shipped commit bumped past it.

### Added

- `--parallel=fork`: collect once in the controller, then fork warm workers that inherit
  the imported suite and the collected items via copy-on-write.
- **Controller-side session fixtures.** `scope="session"` runs once, in the controller,
  before the fork; children inherit the cached result. Under pytest-xdist the same fixture
  runs once per worker. `--parallex-no-session-scope` restores the per-worker behaviour.
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
- **Worker-scoped fixtures.** A `scope="session"` fixture that requests `worker_id`
  (directly or transitively) is per-worker and is never hoisted to the controller, so
  `postgres_server` (one container) and `database_url(worker_id)` (one database per
  worker) can coexist, which is what xdist's filelock recipe exists to fake. `worker_id`
  is provided by parallex when `--parallel` is active, registered late enough to win the
  name from pytest-xdist; a plain `pytest -n 4` run keeps xdist's.

### Requires

- **pytest >= 8.1**, not 8.0: `FixtureManager.getfixturedefs` took a nodeid string until
  8.1 and a `Node` from 8.1 on, and the fork runner passes a `Node`.
- Tested against pytest 8.1, 8.4, 9.0 and 9.1 across all four modes. CI pins the floor.
