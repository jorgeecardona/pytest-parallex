# pytest-parallex

Run pytest in parallel with `scope="session"` fixtures that actually run once.

```bash
pip install pytest-parallex
pytest --parallel=fork
```

This is not mainly a speed tool — see [below](#is-it-faster-than-xdist). It's a simpler
model: one container for the run instead of one per worker, and fixtures you can write the
obvious way.

## Why

xdist starts each worker as a separate interpreter, and each one runs its own session. So a
`scope="session"` fixture runs once per worker. The xdist docs say this and suggest a
`FileLock` workaround. You can't fix it in your conftest — there's no point in xdist's
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

```python
@pytest.fixture(scope="session")
def postgres():
    with PostgresContainer("postgres:16") as pg:   # runs once
        yield pg.get_connection_url()              # every worker gets this URL
```

One container, one login, one compiled asset per run, and no lockfile.

## Modes

```bash
pytest --parallel=fork --parallel-workers=8
```

| mode | isolation | `scope="session"` runs | use when |
|---|---|---|---|
| `fork` | process per worker, warm | once, in the controller | Linux/macOS, and the process is quiet at fork time |
| `thread` | shared address space | once | tests are I/O-bound and don't mind sharing globals |
| `async` | shared address space | once | same as `thread`, driven from an event loop |
| `process` | fresh interpreter per worker | once per worker | tests mutate process-global state, or you're on Windows |

`--parallel-workers` defaults to `auto` (CPU count).

## Is it faster than xdist?

Usually not by much, and sometimes it's slower. Speed isn't the reason to use this.

Both tools run your tests on N processes, so the parallelism is the same. Measured on 16
cores:

| suite | xdist | parallex fork | |
|---|---|---|---|
| 4 modules, 64 quick tests | 1.64s | 1.13s | fork saves worker startup |
| 4 modules, 32 slow tests | 2.75s | **4.32s** | **fork is 1.6x slower** |
| 8 modules, 2s session fixture | 3.16s | 2.49s | barely different |

The one where fork loses is the important one to understand. xdist hands out individual
tests; fork hands out whole modules. So if you have fewer modules than cores, fork can't
fill the machine — 4 modules means 4 workers no matter what you pass to
`--parallel-workers`.

And the row that looks like it should be a blowout isn't. xdist boots that 2-second fixture
in all 8 workers, but it boots them *at the same time*, on cores that were idle anyway. 8
boots cost about 2 seconds of wall clock, not 16. Running it once saves you real CPU, but
CPU you weren't using isn't time you get back. (Same for imports — that's why fork's
startup saving is a flat ~1.5–2.3s that doesn't grow with your suite. On a 3s local run you
notice it; on a 292s CI suite it's under 1%.)

## So why use it?

Because `scope="session"` means session, and that gets you two things.

**One copy of the resource, not N.** Eight xdist workers boot eight Postgres containers.
That's 8x the memory, 8x the disk, 8x the pressure on the Docker daemon — and if you're
paying per seat, per API call, or per licence, 8x that too. Here it's one, and each worker
gets a database on it. The wall clock barely moves; the resource bill does.

**You write the fixture the obvious way.** No `FileLock`, no shared tmpdir, no "am I worker
gw0" branch. The fixture that says it runs once runs once.

If your session fixtures are cheap and your suite has plenty of modules, xdist is fine and
you should keep using it.

[docs/benchmarks.md](docs/benchmarks.md) has the method and three measurements that looked
convincing and were wrong.

## Fork safety

`fork()` only copies the calling thread. Whatever the other threads were holding — a lock,
a half-written buffer, a connection mid-handshake — gets copied in that state and belongs
to nobody in the child. This is the usual way forked children deadlock, and CPython 3.12
warns about it.

parallex checks first and refuses, naming what's in the way:

```
--parallel=fork requires a quiet process at fork time, but 1 non-main thread(s)
are running (Thread-1 (_monitor)). Move the offending setup into a fixture so it
runs after the fork, or use --parallel=process.
```

Logging `QueueListener` threads are the common case (litestar starts one at import, as does
anything using `QueueHandler`), so those get stopped and restarted around the fork for you.

This puts one constraint on a session fixture: what it builds has to survive a fork. An
address survives — a URL, a path, a port. A live connection, a thread, or an event loop
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

Some session fixtures are supposed to differ between workers — usually one that gives each
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

`worker_id` is xdist's fixture name and that's on purpose — it's what lets existing suites
work unchanged. If you have both plugins installed, `--parallel` takes the name so you get
a real per-worker id, and a plain `pytest -n 4` still gets xdist's. `parallex_worker_id` is
an alias if you'd rather be explicit; it marks a fixture per-worker the same way.

## Fixtures

| fixture | gives you |
|---|---|
| `worker_id` | `'f0'`, `'w1'`, `'a2'`… or `'main'`. Asking for it marks a fixture per-worker |
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
- fork splits work by module, so a suite in a single file uses one worker regardless of
  `--parallel-workers`.
- fork is Linux and macOS only. On Windows use `--parallel=process`.
- thread and async don't get around the GIL. They help suites that wait, not suites that
  compute.
- Needs pytest 8.1 or newer. (8.0 changed the signature of an internal we rely on.)

## Developing

```bash
uv sync                # toolchain
make install-hooks     # one-time, after cloning
make check             # lint + typecheck + test, same as CI
```

## Releasing

The version in `pyproject.toml` is the source of truth, and releases are automated:

1. The `pre-commit` hook bumps the patch version when a commit touches `src/` (run
   `make install-hooks` once after cloning). Doc, test and config commits don't bump. For a
   minor or major release, do it deliberately: `make bump TYPE=minor`.
2. `version-guard` enforces the same rule in CI, so `--no-verify` and unhooked clones don't
   get around it.
3. `:release` runs on a green `main` pipeline and publishes via OIDC trusted publishing if
   the version isn't on PyPI yet. Merging a version bump to `main` is the release — no tag,
   no second pipeline.

## License

MIT
