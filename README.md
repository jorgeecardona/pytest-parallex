# pytest-parallex

Parallel test execution for pytest, with one idea the other plugins can't offer:
**`scope="session"` means session — once, not once per worker.**

```bash
pip install pytest-parallex
pytest --parallel=fork
```

## The problem it solves

Under pytest-xdist, every worker is a fresh interpreter that runs its own full session, so
a `scope="session"` fixture executes **once per worker**. Their docs say so plainly, and
ship a `FileLock`-and-shared-tmpdir recipe as the workaround. It isn't a bug in your test
suite — their architecture has no moment at which a controller could build something once
and hand it to everybody.

Forking has that moment.

```python
@pytest.fixture(scope="session")
def postgres():
    with PostgresContainer("postgres:16") as pg:   # boots ONCE
        yield pg.get_connection_url()              # every worker gets this URL
```

`--parallel=fork` sets session fixtures up in the controller *before* forking. Children
inherit the `FixtureDef` — cached result and all — through copy-on-write, so they use it
and never re-run setup. One container, one compiled asset, one login, shared by every
worker, with no lockfile.

Same suite, same four workers, counting how many times the fixture body actually ran:

```console
$ pytest -n 4                                  # pytest-xdist
8 passed        session fixture booted 4 times

$ pytest --parallel=fork --parallel-workers=4  # pytest-parallex
8 passed        session fixture booted 1 time
```

That leaves the scope pytest never had a name for: **the worker**. What xdist calls
"session" is really per-worker, and conflating the two is what forces the recipe. Here
they're separate — `session` runs in the controller, and each forked child is a worker.

## Modes

```bash
pytest --parallel=fork --parallel-workers=8
```

| mode | isolation | `scope="session"` runs | use when |
|---|---|---|---|
| `fork` | process per worker, warm | **once, in the controller** | Linux/macOS, and the process is quiet at fork time |
| `thread` | shared address space | once | tests are I/O-bound and tolerate sharing globals |
| `async` | shared address space | once | same as `thread`, dispatched via asyncio |
| `process` | fresh interpreter per worker | once **per worker** | tests mutate process-global state, or you're on Windows |

`--parallel-workers` defaults to `auto` (CPU count).

## What fork actually costs and buys

Measured, and worth being precise about, because the intuitive pitch is wrong:

- **Removing xdist's worker boot is a flat ~1.5–2.3s.** It does *not* grow with suite size,
  import weight, or worker count. On a 3s local run that's 1.84x and you feel it; on a 292s
  CI suite it's under 1% and it's noise. Redundant imports parallelize across idle cores —
  xdist's N-way collection is wasted *CPU*, not wasted *time*, and you only get paid for
  removing it if the cores were already busy.
- **Session-fixture reuse is the part that scales.** A 10s container boot across 8 workers
  is 80s under xdist and 10s here. That's the reason to reach for this.

Full method, numbers, and three traps that produced convincing wrong answers:
[docs/benchmarks.md](docs/benchmarks.md).

## Fork safety

`fork()` duplicates only the calling thread. Anything another thread held — a lock, a
half-written buffer, a connection's state machine — is duplicated mid-flight and belongs
to nobody in the child. That's how forked children deadlock, and CPython 3.12 warns about
exactly it.

So parallex refuses rather than warns, and names the offender:

```
--parallel=fork requires a quiet process at fork time, but 1 non-main thread(s)
are running (Thread-1 (_monitor)). Move the offending setup into a fixture so it
runs after the fork, or use --parallel=process.
```

Logging `QueueListener` threads (which litestar, and anything using `QueueHandler`, starts
at import) are stopped and restarted around the fork automatically.

The constraint this imposes on a pre-fork session fixture: whatever it builds must survive
a fork. **An address survives — a URL, a path, a port. A live connection, a thread, or an
event loop does not.** The natural split is that the controller owns the *server* and each
worker opens its own *connection* to it:

```python
@pytest.fixture(scope="session")
def db_url():                     # controller: owns the server. Forkable.
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url()

@pytest.fixture                   # worker: owns the connection. Not forkable, so per-test.
def db(db_url):
    conn = psycopg.connect(db_url)
    yield conn
    conn.close()
```

If a session fixture can't survive the fork, `--parallex-no-session-scope` defers them all
to the workers (restoring xdist's once-per-worker behaviour).

## The fixture that has to differ per worker

Some `scope="session"` fixtures exist precisely *to* differ between workers — the archetype
being one that carves a database per worker so tests don't tread on each other. Hoisting
that one would be actively wrong: every worker gets the same database, and a per-test
`create_all`/`drop_all` then wipes its siblings' schemas mid-test.

**A fixture that requests `worker_id` — directly or through another fixture — is
per-worker, and is never hoisted.** Everything else that says `scope="session"` means it.
No new syntax, because suites already write it this way:

```python
@pytest.fixture(scope="session")
def postgres_server():                          # -> controller. One container.
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url()

@pytest.fixture(scope="session")
def database_url(worker_id, postgres_server):   # -> per worker. Requests worker_id.
    url = f"{postgres_server}/test_{worker_id}"
    create_database(url)
    yield url
    drop_database(url)
```

One container for the run, one database per worker on it — which is the thing xdist's
filelock recipe exists to fake. This names the scope xdist conflates: what it calls
"session" is really per-worker, and here you can have both.

`worker_id` is also xdist's fixture name, deliberately — it's what makes existing suites
work unchanged. When both plugins are installed, `--parallel` takes the name so you get a
real per-worker id; a plain `pytest -n 4` run leaves xdist's alone. `parallex_worker_id`
is an alias if you'd rather be explicit, and marks a fixture per-worker just the same.

## Fixtures

| fixture | gives you |
|---|---|
| `worker_id` | `'f0'`, `'w1'`, `'a2'`… or `'main'`. Requesting it marks a fixture per-worker |
| `parallex_worker_id` | the same value, under a name xdist can't shadow |
| `parallex_mode` | the active mode, or `None` |
| `parallex_setup_data` | whatever `pytest_parallex_setup` returned |

## Hooks

```python
# conftest.py
def pytest_parallex_setup(config):
    """Run once in the controller, before any worker starts. Pre-fork: no I/O handles."""
    return {"token": build_expensive_thing()}

def pytest_parallex_teardown(config, data):
    """Run once in the controller, after every worker has finished."""

def pytest_parallex_auto_num_workers(config):
    """Override --parallel-workers=auto. Default: os.cpu_count()."""
    return 8
```

## Known limitations

- **`--maxfail` doesn't stop a fork run early.** Workers run their claimed group to
  completion and the controller replays the reports afterwards, so the count is right but
  the run isn't cut short.
- **Fork parallelizes by module.** A whole suite in one file uses one worker no matter what
  `--parallel-workers` says.
- **`fork` is Linux/macOS only.** On Windows, use `--parallel=process`.
- **`thread`/`async` don't escape the GIL.** They're for suites that wait, not suites that
  compute.

## Developing

```bash
uv sync                # toolchain
make install-hooks     # one-time, after cloning
make check             # lint + typecheck + test, exactly what CI runs
```

## Releasing

The version in `pyproject.toml` is the single source of truth, and releases are automated:

1. **`pre-commit` auto-bumps** the patch version whenever a commit touches `src/` (run
   `make install-hooks` once after cloning). Doc/test/config-only commits don't bump. For
   an intentional minor/major release, bump deliberately: `make bump TYPE=minor`.
2. **`version-guard`** (part of `moon run :ci`) enforces the same rule non-bypassably in
   CI: a push or MR that changes `src/` without a version bump fails the pipeline, catching
   `--no-verify` and unhooked clones.
3. **`:release`** runs on a green `main` pipeline: if `pyproject`'s version isn't on PyPI
   yet, it publishes via OIDC Trusted Publishing. So merging a version bump to `main`
   releases itself — no second pipeline, no tag required.

## License

MIT
