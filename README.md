<h1 align="center">pytest-parallex</h1>

<p align="center"><strong>Run pytest in parallel with <code>scope="session"</code> fixtures that run once per run, not once per worker.</strong></p>

<p align="center">
  <a href="https://pypi.org/project/pytest-parallex/"><img alt="PyPI" src="https://img.shields.io/pypi/v/pytest-parallex?style=flat-square"></a>
  <a href="https://pypi.org/project/pytest-parallex/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/pytest-parallex?style=flat-square"></a>
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/"><img alt="Docs" src="https://img.shields.io/badge/docs-cardona--oss.gitlab.io%2Fpytest--parallex-blue?style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/pytest-parallex/-/pipelines"><img alt="Pipeline" src="https://img.shields.io/gitlab/pipeline-status/cardona-oss%2Fpytest-parallex?branch=main&style=flat-square"></a>
  <a href="https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square"></a>
</p>

<p align="center">
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/#quickstart">Quickstart</a> ·
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/#modes">Modes</a> ·
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/#limitations">Limitations</a> ·
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/benchmarks/">Benchmarks</a> ·
  <a href="https://cardona-oss.gitlab.io/pytest-parallex/changelog/">Changelog</a>
</p>

---

## Overview

`--parallel=fork` collects the tests, runs the session fixtures once, then forks the workers.
Each worker uses the fixtures already built, so one Postgres container serves the run.
pytest-xdist runs a `scope="session"` fixture once per worker.

```bash
pip install pytest-parallex
pytest --parallel=fork
```

Fork mode is for Linux. On macOS it runs with caveats, and `--parallel=process` is the
dependable mode there (see [Limitations](#limitations)). Windows has no `fork`: use
`--parallel=process`, which no CI job tests on Windows. `process` mode runs a session fixture
once per worker, as xdist does.

It is also usually [faster than xdist](#is-it-faster-than-xdist): a forked worker starts from a
process that already imported the suite, so no worker boots an interpreter.

CI runs the suite on Python 3.10 to 3.15 on Linux and 3.12 to 3.15 on macOS. Status: beta.

## Why pytest-parallex

xdist starts each worker as a separate interpreter, and each one runs its own session. So a
`scope="session"` fixture runs once per worker. The xdist docs say this and suggest a
`FileLock` workaround. You can't fix it in your conftest: there's no point in xdist's
design where one process could build something and hand it to the others.

Forking gives you that point. `--parallel=fork` collects the tests, runs your session
fixtures, and then forks. Each child inherits the already-built fixtures through
copy-on-write and uses them without re-running anything.

Counting how many times the fixture body actually runs:

```console
$ pytest -n 4                                  # pytest-xdist
8 passed        session fixture ran 4 times

$ pytest --parallel=fork --parallel-workers=4  # pytest-parallex
8 passed        session fixture ran 1 time
```

`scope="session"` meaning session gets you two things:

- One copy of the resource, not N. Eight xdist workers boot eight Postgres containers:
  8x the memory, 8x the disk, 8x the pressure on the Docker daemon, and if you pay per seat,
  per API call, or per licence, 8x that too. Here it's one, and each worker gets a database
  on it. The wall clock barely moves; the resource bill does.
- A plain fixture. No `FileLock`, no shared tmpdir, no "am I worker gw0" branch. A fixture
  declared `scope="session"` runs once.

What it does not do:

- Fork everywhere. Fork mode is for Linux. Windows needs `--parallel=process`. macOS works
  with hardening, but `process` is the dependable mode there (see [Limitations](#limitations)).
- Split a module that owns a module- or class-scoped fixture. That module stays on one
  worker, which keeps its fixture set up once.
- Get around the GIL. `thread` and `async` help suites that wait, not suites that compute.
- Stop early on `--maxfail` under fork. The count is right, but the run is not cut short.

## Install

```bash
pip install pytest-parallex      # or: uv add --dev pytest-parallex
```

Python 3.10+, pytest 8.1+. The only runtime dependency is pytest.

## Quickstart

One Postgres container for the whole run, every worker inherits its URL:

```python
# conftest.py
import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def db_url():
    with PostgresContainer("postgres:16") as pg:   # runs once, in the controller
        yield pg.get_connection_url()              # every worker gets this URL
```

```bash
pytest --parallel=fork --parallel-workers=4
```

The run ends with one line that says what the plugin did:

```text
pytest-parallex fork: 4 worker(s), 8 claim(s) | session fixtures: 1 hoisted
```

`hoisted` is the count of session fixtures that ran once in the controller. `per-worker` counts
the ones left to the workers on purpose (see
[Fixtures that differ per worker](#fixtures-that-differ-per-worker)). If a fixture could not be set up pre-fork the line says
`N FAILED to hoist (ran per-worker; PARALLEX_TRACE=1 for why)`; set `PARALLEX_TRACE=1` to see
the reason on stderr.

## Key features

- Session fixtures run once. Hoisted into the controller before the fork, inherited by every
  worker through copy-on-write.
- Per-worker fixtures when you want them. Ask for `worker_id` and the fixture stays in the
  workers. It is xdist's fixture name on purpose, so existing suites work unchanged.
- Four modes: `fork`, `thread`, `async`, `process`; `--parallel-workers` defaults to `auto`
  (CPU count).
- Fork-safety check. Refuses to fork over a non-main thread and names it. Logging
  `QueueListener` threads are stopped and restarted around the fork for you.
- macOS hardening. The `_scproxy` proxy lookup and libinfo DNS resolution
  (`socket.getaddrinfo`), the two paths that segfault a forked child, are disarmed in workers
  automatically.
- Test-by-test dealing. A module with only function and session fixtures is spread across
  all workers, so one big file fills the machine.
- Coverage that counts worker lines. Explicit save in forked workers; per-worker
  `COVERAGE_FILE` combined on the controller in process mode.
- Private first-import caches. Each forked worker gets its own `MPLCONFIGDIR`,
  `NUMBA_CACHE_DIR` and pytensor compiledir, so cold CI caches don't race.
- Hooks: `pytest_parallex_setup`, `pytest_parallex_teardown`,
  `pytest_parallex_auto_num_workers`.

## Modes

```bash
pytest --parallel=fork --parallel-workers=8
```

| mode | isolation | `scope="session"` runs | use when |
|---|---|---|---|
| `fork` | process per worker, warm | once, in the controller | Linux (macOS: see [Limitations](#limitations)), no extra thread running at fork time |
| `thread` | shared address space | once | tests are I/O-bound and don't mind sharing globals |
| `async` | shared address space | once | same as `thread`, driven from an event loop |
| `process` | fresh interpreter per worker | once per worker | tests mutate process-global state, macOS, or Windows (not tested in CI) |

`--parallel-workers` defaults to `auto` (CPU count).

## Is it faster than xdist?

Usually yes, though speed isn't the main reason to reach for it.

Both tools run your tests on N processes. The difference is startup: xdist spawns a fresh
interpreter per worker and re-imports your suite in each; fork forks warm from a process
that already imported everything, at about a millisecond per worker. So when both fill the
machine, fork tends to come out ahead. Measured on 2026-07-16 with pytest-parallex 0.1.6 on a
16-core machine:

| suite | xdist | parallex fork | |
|---|---|---|---|
| 4 modules, 64 quick tests | 1.39s | 0.33s | fork skips 16 interpreter boots |
| 4 modules, 32 slow tests | 2.62s | 1.30s | ~2x |
| 1 module, 16 slow tests | 2.02s | 0.76s | one big module, split per test |

In the last row, all 16 tests sit in one module. A module whose tests use only function and
session fixtures is dealt test-by-test, so it fills the machine like xdist does, without the
per-worker interpreter startup.

The limit that remains: a module that owns a module- or class-scoped fixture
stays on one worker, because that is what keeps the fixture set up once for the module.
Such a module can't use more than one core no matter what you pass to `--parallel-workers`.
If one giant module with a module fixture dominates your runtime, xdist (which re-runs that
fixture per worker) can still spread it wider, at the cost of running it N times.

The [benchmarks page](https://cardona-oss.gitlab.io/pytest-parallex/benchmarks/) has the
method, the limits of these numbers, and the earlier measurements that were wrong.

## Fork safety

`fork()` only copies the calling thread. Whatever the other threads were holding (a lock,
a half-written buffer, a connection mid-handshake) gets copied in that state and belongs
to nobody in the child. This is the usual way forked children deadlock, and CPython 3.12
warns about it.

parallex checks first and refuses, naming what's in the way:

```text
--parallel=fork requires a quiet process at fork time, but 1 non-main thread(s)
are running (Thread-1 (_monitor)). Move the offending setup into a fixture so it
runs after the fork, or use --parallel=process.
```

Logging `QueueListener` threads are the common case (litestar starts one at import, as does
anything using `QueueHandler`), so those get stopped and restarted around the fork for you.

This puts one constraint on a session fixture: what it builds has to survive a fork. An
address survives: a URL, a path, a port. A live connection, a thread, or an event loop
doesn't. So keep the server in the controller and open connections per worker:

```python
@pytest.fixture(scope="session")
def db_url():                     # controller: owns the server, survives the fork
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url()

@pytest.fixture                   # per test: owns the connection, can't survive a fork
def db(db_url):
    conn = psycopg.connect(db_url)
    yield conn
    conn.close()
```

If a session fixture can't survive the fork, `--parallex-no-session-scope` leaves them all
to the workers and you're back to xdist's behaviour.

## Fixtures that differ per worker

Some session fixtures are supposed to differ between workers, usually one that gives each
worker its own database. Running that in the controller breaks things: every worker gets
the same database, and a per-test `create_all`/`drop_all` wipes the other workers' schemas
while they're using them.

A fixture that asks for `worker_id`, directly or through another fixture, is per-worker and
stays in the workers. Everything else scope-session runs once in the controller. There's no
new syntax for this because suites already write it:

```python
@pytest.fixture(scope="session")
def postgres_server():                          # controller: one container
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url()

@pytest.fixture(scope="session")
def database_url(worker_id, postgres_server):   # per worker: asks for worker_id
    url = f"{postgres_server}/test_{worker_id}"
    create_database(url)
    yield url
    drop_database(url)
```

One container for the run, one database per worker on it. Under xdist you'd need the
filelock recipe to approximate this.

`worker_id` is xdist's fixture name and that's on purpose: it's what lets existing suites
work unchanged. If you have both plugins installed, `--parallel` takes the name so you get
a real per-worker id, and a plain `pytest -n 4` still gets xdist's. `parallex_worker_id` is
an alias if you'd rather be explicit; it marks a fixture per-worker the same way.

## Fixtures

| fixture | gives you |
|---|---|
| `worker_id` | `'f0'`, `'w1'`, `'a2'`... or `'main'`. Asking for it marks a fixture per-worker |
| `parallex_worker_id` | the same value, under a name xdist can't shadow |
| `parallex_mode` | the active mode, or `None` |
| `parallex_setup_data` | whatever `pytest_parallex_setup` returned |

## Hooks

```python
# conftest.py
def pytest_parallex_setup(config):
    """Runs once in the controller, before any worker starts. Pre-fork, so no I/O handles."""
    return {"token": build_expensive_thing()}

def pytest_parallex_teardown(config, data):
    """Runs once in the controller, after every worker has finished."""

def pytest_parallex_auto_num_workers(config):
    """Override --parallel-workers=auto. Defaults to os.cpu_count()."""
    return 8
```

## Limitations

- `--maxfail` won't cut a fork run short. Workers finish the group they claimed and the
  controller replays the reports afterwards, so the count is right but the run isn't
  stopped early.
- A module that owns a module- or class-scoped fixture runs on one worker (that's what
  keeps the fixture set up once for it). Modules using only function and session fixtures
  are split across workers freely.
- Fork mode is for Linux. On Windows use `--parallel=process` (not tested in CI). On macOS,
  forked children can be killed by Apple frameworks: you see a "Python quit unexpectedly" dialog per
  worker and `no tests ran`. Three distinct traps, diagnosed on real suites:

  * **`_scproxy` segfault**: urllib's proxy lookup calls the
    SystemConfiguration framework, which is not fork-safe. botocore reaches it on every
    client creation, so any AWS test can trigger it ([CPython #105912](https://github.com/python/cpython/issues/105912),
    [bpo-31818](https://bugs.python.org/issue31818)). **The plugin disarms this one
    automatically in workers** by stubbing the `*_macosx_sysconf` leaves, which the
    original functions resolve at call time, so it holds even for code that captured
    `proxy_bypass` by value at import (botocore does), and even for tests that wipe
    `os.environ`. Verified on a 530-test suite: zero dead workers, and fork became the
    fastest configuration on the machine (30.0s vs xdist's 38.2s). Workers therefore see
    "no proxy configured"; a worker that must exercise real macOS proxy discovery can't
    run under fork at all.
  * **libinfo DNS segfault**: `socket.getaddrinfo` on a real hostname dies in a forked
    child once the parent has resolved any hostname (pulling a container image is enough).
    **Also disarmed automatically in workers**: numeric addresses stay on the real
    `getaddrinfo`; real names resolve in a short-lived `python -I -c` subprocess, cached
    per endpoint (one spawn per worker, ~50ms, once).
  * **The ObjC `+initialize` trap**: the runtime crashes a forked child touching a
    framework initialized pre-fork. Not disarmable from inside a running process;
    `export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` (in your shell profile, before
    Python starts) is Apple's escape hatch. Note it does **not** fix the other two traps:
    on the suites that hit them, it had no effect.

  This is why multiprocessing defaults to spawn on macOS since Python 3.8. If workers
  still die, `--parallel=process` is the dependable mode on macOS (530-test suite:
  process 37.9s vs xdist 38.2s vs serial ~133s; you lose fork's shared session fixtures,
  not the parallelism).
- `thread` and `async` don't get around the GIL. They help suites that wait, not suites that
  compute.
- Needs pytest 8.1 or newer. pytest 8.1 changed `FixtureManager.getfixturedefs` to take a
  `Node`, and fork mode passes one.
- Upgrade from versions before 0.2.4 if you use fork mode. There, a failing comparison
  (`assert a == b`, with or without a message) printed a bare `AssertionError` raised inside
  pytest, without the message or the diff
  ([#2](https://gitlab.com/cardona-oss/pytest-parallex/-/issues/2),
  [#4](https://gitlab.com/cardona-oss/pytest-parallex/-/issues/4); seen with pytest 9.0).

## Coverage

Under fork, workers are forked children, and two things make coverage undercount with no
warning: coverage restarts collection in a forked child only when told how, and the worker exits via
`os._exit`, which skips the exit hook coverage saves from (the plugin saves explicitly for
you). The working recipe, with which measured missing lines went from "everything workers
ran" to zero:

```toml
# pyproject.toml
[tool.coverage.run]
patch = ["fork"]
parallel = true
```

```bash
COVERAGE_PROCESS_START=pyproject.toml pytest --parallel=fork --cov
```

The env var is coverage's own switch for measuring in child processes; without it,
`patch = fork` silently does nothing and every line that ran only in a worker counts as
unexecuted. If your CI gates on coverage, set it in the job env.

Under `--parallel=process`, each worker measures into its own `COVERAGE_FILE` and the
controller combines them at session end, so `--cov-fail-under` is judged against the
combined total, not against one worker's slice.

## First-import caches (matplotlib, numba, pytensor)

Some libraries build a cache on the **first import in a process**: matplotlib's font cache
(`MPLCONFIGDIR`), numba (`NUMBA_CACHE_DIR`), pytensor's compiledir (pymc). When several
processes build the same cold cache at once they can trip over each other's half-written
files, the same class of race the per-worker `COVERAGE_FILE` namespace fixes for coverage.
It is a CI-only shape: a developer's cache is warm from months of use, so it "passes locally,
fails in CI" (a cold `MPLCONFIGDIR` with N forked importers reproduces "building the font
cache" N times).

Under `--parallel=fork` the plugin removes the race automatically: each worker is given a
private cache dir (`MPLCONFIGDIR`, `NUMBA_CACHE_DIR`, pytensor `base_compiledir`) before it
runs a test, and the controller deletes them at the end. The cost is that each worker builds
its own copy of the cache.

To pay that build **once** instead of per worker, import the library in the controller before
the fork. A hoisted `scope="session"` fixture (or a `pytest_parallex_setup` hook) does this,
and every child then inherits the warm cache through the fork:

```python
@pytest.fixture(scope="session", autouse=True)
def _warm_import_caches():
    import matplotlib.pyplot  # noqa: F401  builds the font cache once, pre-fork
```

`--parallel=process` starts a fresh interpreter per worker, so there is no pre-fork controller
to warm and no inherited cache; if a process-mode suite hits the same race, point each of those
env vars at a per-worker path in your CI job.

## Links

- Repository: <https://gitlab.com/cardona-oss/pytest-parallex>
- PyPI: <https://pypi.org/project/pytest-parallex/>
- Documentation: <https://cardona-oss.gitlab.io/pytest-parallex/>
- Changelog: <https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/CHANGELOG.md>
- Issues: <https://gitlab.com/cardona-oss/pytest-parallex/-/issues>

## Contributing

Toolchain, CI, and the release process live in
[CONTRIBUTING.md](https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/CONTRIBUTING.md);
build and test detail for coding agents is in
[AGENTS.md](https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/AGENTS.md). In short:

```bash
uv sync                # toolchain
make install-hooks     # one-time, after cloning
make check             # lint + typecheck + test (CI also runs the floor, build and docs)
```

The version is `__version__` in `src/pytest_parallex/__init__.py`; the pre-commit hook bumps
it when a commit touches `src/`, and merging that bump to `main` is the release (OIDC trusted
publishing, no tag, no manual step).

## License

MIT © Jorge Cardona. See
[LICENSE](https://gitlab.com/cardona-oss/pytest-parallex/-/blob/main/LICENSE).
