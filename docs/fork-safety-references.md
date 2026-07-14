# fork() safety — curated references for a forking pytest worker plugin

*2026-07-14. Source-grounded research for a pytest plugin that forks a pre-warmed parent to create
test workers. Every claim below is a verbatim quote from a primary source with a URL. Follows the
[[search-first-for-infra]] rule: design from the docs, cite them, then verify with a canary — never
discover documented behavior by canary.*

**Bottom line:** the design "fork a pre-warmed parent that has already opened resources" is the exact
pattern every primary source warns against. The salvage is `forkserver` +
`set_forkserver_preload` — stdlib-blessed pre-warming that is single-threaded by construction — with
the hard rule that **preloaded modules import code but open no connections, loops, or threads**.

---

## 1. The two independent hazard classes

They compound but are distinct — fixing one does not fix the other:

| Class | Trigger | Failure mode |
|---|---|---|
| **Shared fd / duplicated state** | fork with a connection, epoll fd, or socket open | Two processes on one TCP socket → interleaved protocol messages, corrupted session |
| **Locks held at fork instant** | fork while >1 thread alive | Child deadlocks forever on a lock no surviving thread will release (malloc, logging, import) |

---

## 2. PostgreSQL / libpq — the authority under every driver

<https://www.postgresql.org/docs/current/libpq-connect.html> (§ Database Connection Control Functions):

> On Unix, forking a process with open libpq connections can lead to unpredictable results because
> the parent and child processes share the same sockets and operating system resources. For this
> reason, such usage is not recommended, though doing an `exec` from the child process to load a new
> executable is safe.

Note the carve-out: **`exec` from the child is safe** (it discards the inherited address space). Fork
*without* exec is the unsafe case — and that is precisely what a forking worker plugin does.

### psycopg2

<https://www.psycopg.org/docs/usage.html> (§ Thread and process safety):

> In DB API 2.0 parlance, Psycopg is level 2 thread safe.

> The above observations are only valid for regular threads: they don't apply to forked processes nor
> to green threads. libpq connections shouldn't be used by a forked processes, so when using a module
> such as multiprocessing or a forking web deploy method such as FastCGI make sure to create the
> connections **after the fork**.

The concrete symptom, from the FAQ — <https://www.psycopg.org/docs/faq.html>:

> **Why do I get the error `current transaction is aborted, commands ignored until end of transaction
> block` when I use multiprocessing (or any other forking system) and not when use threading?**
> Psycopg's connections can't be shared across processes (but are thread safe). If you are forking the
> Python process make sure to create a new connection in each forked child.

This is the fingerprint to watch for in worker logs: a *transaction-aborted* error storm, not a clean
crash. Sharing corrupts the session, it does not fail fast.

### psycopg3

The fork warning is **not on the pool page** (verified: `advanced/pool.html` and `api/pool.html`
contain zero occurrences of "fork"). It lives on the *Concurrent operations* page —
<https://www.psycopg.org/psycopg3/docs/advanced/async.html>:

> **Warning:** Connections are not process-safe and cannot be shared across processes, for instance
> using the facilities of the `multiprocessing` module. If you are using Psycopg in a forking
> framework (for instance in a web server that implements concurrency using multiprocessing), you
> should make sure that the database connections are created after the worker process is forked.
> Failing to do so you will probably find the connection **in broken state**.

psycopg3's pool adds nothing that makes a pool fork-safe; the guidance is unchanged from psycopg2.

### SQLAlchemy — the clearest mechanism statement

<https://docs.sqlalchemy.org/en/20/core/pooling.html> § *Using Connection Pools with Multiprocessing
or os.fork()*:

> It's critical that when using a connection pool, and by extension when using an `Engine` created via
> `create_engine`, that the pooled connections **are not shared to a forked process**. TCP connections
> are represented as file descriptors, which usually work across process boundaries, meaning this will
> cause concurrent access to the file descriptor on behalf of two or more entirely independent Python
> interpreter states.

> Depending on specifics of the driver and OS, the issues that arise here range from non-working
> connections to socket connections that are used by multiple processes concurrently, leading to
> **broken messaging** (the latter case is typically the most common).

Four sanctioned approaches; **#2 is marked "This is the recommended approach"**:

```python
engine = create_engine("...")

def initializer():
    """ensure the parent proc's database connections are not touched
    in the new connection pool"""
    engine.dispose(close=False)

with Pool(10, initializer=initializer) as p:
    p.map(run_in_process, data)
```

`close=False` (added 1.4.33) replaces the child's pool **without** closing the parent's live
connections — closing them from the child would tear down sockets the parent is still using. Getting
this wrong is a classic: `dispose()` (the default, `close=True`) in a child sends protocol close
messages down the *shared* socket and breaks the parent.

The other three: (1) `NullPool`; (3) `engine.dispose()` *before* forking; (4) a `connect`/`checkout`
event-listener pair that stamps `os.getpid()` and raises `DisconnectionError` on mismatch.

The **hard limit** — the escape hatch does not extend to live objects:

> The above strategies will accommodate the case of an `Engine` being shared among processes. The
> above steps alone are **not sufficient** for the case of sharing a specific `Connection` over a
> process boundary; prefer to keep the scope of a particular `Connection` local to a single process
> (and thread). It's additionally **not supported to share any kind of ongoing transactional state**
> directly across a process boundary, such as an ORM `Session` object that's begun a transaction and
> references active `Connection` instances; again prefer to create new `Session` objects in new
> processes.

⚠️ **Direct consequence for a pre-warmed test-worker design:** a pre-warmed parent holding an open
transaction (the standard "wrap each test in a rollback" fixture) is in exactly the unsupported
category. An `Engine` can be forked with discipline; **an open transaction cannot.**

---

## 3. fork + threads — CPython's own warning

<https://docs.python.org/3/library/os.html#os.fork>, verbatim:

> **Changed in version 3.12:** If Python is able to detect that your process has multiple threads,
> `os.fork()` now raises a `DeprecationWarning`.
>
> We chose to surface this as a warning, when detectable, to better inform developers of a design
> problem that the POSIX platform specifically notes as not supported. **Even in code that *appears*
> to work, it has never been safe to mix threading with `os.fork()` on POSIX platforms.** The CPython
> runtime itself has always made API calls that are not safe for use in the child process when threads
> existed in the parent (such as `malloc` and `free`).
>
> Users of macOS or users of libc or malloc implementations other than those typically found in glibc
> to date are among those already more likely to experience deadlocks running such code.

Also on the same page:

> Note that some platforms including FreeBSD <= 6.3 and Cygwin have known issues when using `fork()`
> from a thread.

**Trigger:** the warning fires on `os.fork()` when CPython detects **more than one thread alive** — it
is not about *which* thread forks. Issue: <https://github.com/python/cpython/issues/100228>, "raise a
Warning when os.fork() is called and the process has multiple threads", opened by **gpshead** (Gregory
P. Smith), closed 2023-01-29, implemented in gh-100229. Not a PEP.

**Why** — from the discussion the docs themselves link, <https://discuss.python.org/t/33555>
("Concerns regarding deprecation of fork() with alive threads"): POSIX permits only
**async-signal-safe** operations in the child until `exec`; `pthread_mutex_lock`/`unlock` are *not*
async-signal-safe. Only the forking thread survives into the child — any lock held by any *other*
thread at the fork instant is **left locked forever**, with no thread alive to release it. The child
then deadlocks the first time it touches `malloc`, `logging`, or the import lock. gpshead's framing:
the CPython runtime "is by definition unsafe for use after `fork()`."

**This is the trap for a pre-warmed parent.** Warming is exactly what starts threads — a driver
keepalive, a `ThreadPoolExecutor`, an OTel/logging exporter, gRPC. The parent looks healthy; the
children deadlock non-deterministically, mostly under load, mostly in CI.

### TLS — a separate fork bug

`os.fork` cross-references <https://docs.python.org/3/library/ssl.html>:

> If using this module as part of a multi-processed application (using, for example the
> `multiprocessing` or `concurrent.futures` modules), be aware that OpenSSL's internal random number
> generator **does not properly handle forked processes**. Applications must change the PRNG state of
> the parent process if they use any SSL feature with `os.fork()`. Any successful call of `RAND_add()`
> or `RAND_bytes()` is sufficient.

Relevant if workers talk TLS to Postgres (`sslmode=require`): forked children otherwise share PRNG
state.

---

## 4. asyncio + fork

**There is no official statement in the asyncio docs.** Verified: `asyncio-dev.rst`,
`asyncio-eventloop.rst`, `asyncio-runner.rst` contain **zero** occurrences of "fork". The authority is
the issue tracker and the source.

**Guido van Rossum**, closing <https://github.com/python/cpython/issues/85789> ("What's the by-design
behavior when `os.fork()` is invoked in an asyncio loop?") as *not a bug*, 2020-08-24 (typos his):

> As you,can tell from thencode this by design. The child,must create a new event loop
> explicitly,ifmitmwants to use asyncio, and all,existing Futures are off limits.
>
> Fork is dangerous. Don't use unless you understand it.

**The fd-sharing mechanism**, demonstrated in <https://github.com/python/cpython/issues/66285> — the
self-pipe is *duplicated, not recreated*:

```python
loop = asyncio.get_event_loop()
pid = os.fork()
# parent 6 5
# child  6 5     <-- same _csock/_ssock fds in both processes
```

Tracked as bpo-21998 → <https://github.com/python/cpython/issues/66197>, "asyncio: support fork" —
*"a new self-pipe should be created in the child process after fork"*. The epoll/kqueue selector fds
are inherited the same way; a forked child manipulating them affects the parent's epoll set.

**What CPython actually does today** — `Lib/asyncio/events.py`, verbatim:

```python
if hasattr(os, 'fork'):
    def on_fork():
        # Reset the loop and wakeupfd in the forked child process.
        global _local
        _local = _Local()
        _set_running_loop(None)
        signal.set_wakeup_fd(-1)

    os.register_at_fork(after_in_child=on_fork)
```

Shipped via gh-99539 ("GH-66285: fix forking in asyncio", kumaraditya303, merged 2022-11-24 → 3.12),
which per its author does two things: reset the event-loop registry so parent and child don't share a
loop, and reset the `wakeupfd` so signals to the child don't run the parent's handler.

**Scope this precisely — it is easy to over-read.** The handler clears the *loop registry* and the
*wakeup fd*. It does **not** close the inherited epoll fd or the parent's self-pipe socketpair, and it
does not make inherited `Future`/`Task` objects usable. It makes `get_event_loop()` in the child
return a *fresh* loop instead of the parent's. Guido's constraint stands: the child must build a new
loop, and existing Futures are off limits. Note the 3.12 handler landed only after the 2018 fix was
**reverted** (Yury: "We should strive to implement a proper solution, not commit some half-working
code") and the issue sat open from 2014 to 2022 — this area has a long history of partial fixes.

---

## 5. The documented safe discipline

Uniform across every source: **fork first, open second.**

- libpq: `exec` from the child is safe; forking with open connections is "not recommended".
- psycopg2/3: "make sure to create the connections **after the fork**".
- SQLAlchemy: `engine.dispose(close=False)` in the child's **initialize phase**.
- asyncio: the child **must create a new event loop explicitly**.
- CPython: don't fork with >1 thread alive.

### `os.register_at_fork` — the mechanism, and its limits

<https://docs.python.org/3/library/os.html#os.register_at_fork> (added 3.7), verbatim:

> Register callables to be executed when a new child process is forked using `os.fork()` or similar
> process cloning APIs. The parameters are optional and keyword-only. Each specifies a different call
> point.
>
> - *before* is a function called before forking a child process.
> - *after_in_parent* is a function called from the parent process after forking a child process.
> - *after_in_child* is a function called from the child process.
>
> These calls are only made if control is expected to return to the Python interpreter. A typical
> `subprocess` launch will not trigger them as the child is not going to re-enter the interpreter.
>
> Functions registered for execution before forking are called in reverse registration order.
> Functions registered for execution after forking (either in the parent or in the child) are called
> in registration order.
>
> Note that `fork()` calls made by third-party C code may not call those functions, unless it
> explicitly calls `PyOS_BeforeFork()`, `PyOS_AfterFork_Parent()` and `PyOS_AfterFork_Child()`.
>
> **There is no way to unregister a function.**

Two teeth here for a plugin author: **no unregister** (a plugin registering per-session leaks handlers
across runs), and **third-party C code may bypass it entirely**. `register_at_fork` is a *repair*
hook — it cannot make a lock that was held at the fork instant safe, because the child is already in
the async-signal-unsafe world by the time `after_in_child` runs. It is correct for "drop the inherited
pool / reset the loop"; it is **not** a fix for fork-with-threads.

---

## 6. forkserver — the middle ground, and the actual answer

<https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods>, verbatim:

> ***forkserver***
> When the program starts and selects the *forkserver* start method, a server process is spawned. From
> then on, whenever a new process is needed, the parent process connects to the server and requests
> that it fork a new process. **The fork server process is single threaded unless system libraries or
> preloaded imports spawn threads as a side-effect so it is generally safe for it to use `os.fork()`.
> No unnecessary resources are inherited.**
>
> Available on POSIX platforms which support passing file descriptors over Unix pipes such as Linux.
> The default on those.

And *fork*, for contrast:

> The parent process uses `os.fork()` to fork the Python interpreter. The child process, when it
> begins, is effectively identical to the parent process. **All resources of the parent are inherited
> by the child process.** Note that safely forking a multithreaded process is problematic.

### Python 3.14 default change — VERIFIED

Confirmed verbatim from the 3.14 docs, two independent notes:

> **Changed in version 3.14:** On POSIX platforms the default start method was changed from *fork* to
> *forkserver* to retain the performance but avoid common multithreaded process incompatibilities.
> See gh-84559.

> *fork* — **Changed in version 3.14:** This is no longer the default start method on any platform.
> Code that requires *fork* must explicitly specify that via `get_context()` or `set_start_method()`.

> *forkserver* — **Changed in version 3.14:** This became the default start method on POSIX platforms.

Tracking issue <https://github.com/python/cpython/issues/84559>: *"multiprocessing's default posix
start method of `'fork'` is broken: change to `'forkserver' || 'spawn'`"* — filed by **itamarst**
2020-04-24, **closed 2024-11-11**, implemented in gh-101556 ("Change the multiprocessing start method
default to `forkserver`"). The user's recollection is correct, with a nuance worth keeping: the change
is **POSIX-wide**, not Linux-only (macOS was already `spawn` since 3.8), and the chosen default is
**forkserver**, not `spawn` — explicitly "to retain the performance".

### Pre-warming is a first-class, supported feature

`set_forkserver_preload` is the stdlib's own answer to "pre-warmed parent" —
<https://docs.python.org/3/library/multiprocessing.html#multiprocessing.set_forkserver_preload>:

> Set a list of module names for the forkserver main process to attempt to import so that their
> **already imported state is inherited by forked processes**. This can be used as a performance
> enhancement to avoid repeated work in every process.
>
> For this to work, it must be called **before the forkserver process has been launched** (before
> creating a `Pool` or starting a `Process`).

(3.15 adds `on_error='ignore'|'warn'|'fail'`; default `"ignore"` **silently** swallows `ImportError`
— a preload that fails to import degrades to a cold worker with no signal. Use `"warn"`/`"fail"` when
available.)

**The catch, stated in the docs themselves:** single-threadedness holds *"unless system libraries or
preloaded imports spawn threads as a side-effect"*. So preloading is safe for **import cost**
(SQLAlchemy, pytest plugins, your app's module graph — the bulk of test-suite warm-up) and unsafe for
**live resources**. A preloaded module that starts a thread or opens a connection at import time
silently reintroduces both hazard classes into the fork server.

---

## 7. Design verdict for the plugin

**Fork-the-warmed-parent, as stated, is the anti-pattern.** Each source independently condemns it, and
they compound: warming imports → threads; warming connections → shared sockets; warming an async
runtime → shared epoll + self-pipe.

Split "warm" into two kinds and the design resolves:

| Warm state | Fork-safe? | Discipline |
|---|---|---|
| Imported modules, parsed config, collected test tree, in-memory fixtures | ✅ Yes | `set_forkserver_preload` — this is the sanctioned mechanism, and it's most of the win |
| DB connections / pools | ❌ No | Open **after** fork; `engine.dispose(close=False)` in child init |
| Open transactions, `Session`, `Connection` | ❌ No — explicitly unsupported | Recreate in child; never inherit |
| Event loop, epoll fd, self-pipe | ❌ No | New loop in child; existing Futures off limits |
| Threads (pools, exporters, keepalives) | ❌ No | Must not exist in the forking process |

**Fastest defensible design:** `forkserver` + `set_forkserver_preload([...])` for the import graph,
workers opening their own connections post-fork. This is the stdlib's blessed shape for exactly this
goal, it's the 3.14 default, and it sidesteps the thread hazard by construction.

**Verification, before trusting any of it** (the canary *verifies*, it must not *discover*):

1. `PYTHONWARNINGS=error::DeprecationWarning` → any fork-with-threads becomes a hard, loud failure
   rather than a rare CI deadlock. This is the single highest-value guard.
2. Assert `threading.active_count() == 1` immediately before the fork, and log the thread names when
   it isn't — that turns "mystery hang" into a named culprit.
3. Grep worker logs for `current transaction is aborted, commands ignored until end of transaction
   block` — the documented fingerprint of a shared libpq socket.

---

## 8. Source index

| # | Source | URL |
|---|---|---|
| 1 | libpq — Database Connection Control Functions | <https://www.postgresql.org/docs/current/libpq-connect.html> |
| 2 | psycopg2 — Thread and process safety | <https://www.psycopg.org/docs/usage.html> |
| 3 | psycopg2 — FAQ (transaction-aborted fingerprint) | <https://www.psycopg.org/docs/faq.html> |
| 4 | psycopg3 — Concurrent operations (fork warning) | <https://www.psycopg.org/psycopg3/docs/advanced/async.html> |
| 5 | SQLAlchemy — Pools with Multiprocessing or os.fork() | <https://docs.sqlalchemy.org/en/20/core/pooling.html> |
| 6 | CPython — `os.fork` / `os.register_at_fork` | <https://docs.python.org/3/library/os.html#os.fork> |
| 7 | CPython — multiprocessing contexts and start methods | <https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods> |
| 8 | CPython — `ssl` Multi-processing (OpenSSL PRNG) | <https://docs.python.org/3/library/ssl.html> |
| 9 | gh-100228 — warn on fork with multiple threads (3.12) | <https://github.com/python/cpython/issues/100228> |
| 10 | discuss.python.org t/33555 — fork vs threads rationale | <https://discuss.python.org/t/33555> |
| 11 | gh-84559 — default start method fork → forkserver (3.14) | <https://github.com/python/cpython/issues/84559> |
| 12 | gh-85789 — Guido: "Fork is dangerous" | <https://github.com/python/cpython/issues/85789> |
| 13 | gh-66285 / gh-66197 — asyncio fork + self-pipe | <https://github.com/python/cpython/issues/66285> |
| 14 | gh-99539 — asyncio fork handler (3.12) | <https://github.com/python/cpython/pull/99539> |
| 15 | CPython source — `Lib/asyncio/events.py` `on_fork()` | <https://github.com/python/cpython/blob/main/Lib/asyncio/events.py> |

**Labeled speculation** (not doc-supported, flagged as such): that `forkserver` + preload will be
*fast enough* for the plugin's warm-up budget is an assumption — the docs claim performance parity as
the *rationale* for the 3.14 default, but the actual saving depends on how much of the warm state is
imports (inheritable) vs. connections (not). Measure it. Also unverified: whether the specific test
suite's plugins spawn threads at import time — check with `threading.active_count()` after preload.
