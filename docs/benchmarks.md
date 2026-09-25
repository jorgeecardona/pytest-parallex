# What fork actually buys, measured 2026-07-14

Written after a session that produced three wrong answers before a right one. The wrong turns are
kept here on purpose: each one is a trap that looks exactly like a result.

## Bottom line

**`--parallel=fork` removes xdist's fixed worker-boot cost: a flat ~1.5–2.3s. That is the whole
win.** It does *not* scale with suite size, import weight, or worker count.

That makes fork mode a **local TDD-loop feature, not a CI feature**:

| suite | xdist | fork | verdict |
|---|---|---|---|
| 3s local run | 3.21s | 1.75s | **1.84x, you feel this** |
| reference CI suite (292s) | n/a | −2s | **under 1%. noise.** |

## The thesis that died

The pitch was: *xdist re-imports and re-collects in every worker (its own FAQ admits it, and
[#353](https://github.com/pytest-dev/pytest-xdist/issues/353) reports a 4x collection penalty on a
16k-test suite), so collect-once-then-fork should scale better the heavier the import.*

Measured, with a CPU-bound import (not a `sleep`; see trap 3), 8 workers on 16 cores:

| import cost | xdist -n8 | fork x8 | ratio | absolute saving |
|---|---|---|---|---|
| 0.03s | 3.21s | 1.75s | 1.83x | 1.46s |
| 0.55s | 3.82s | 2.17s | 1.76x | 1.65s |
| 2.01s | 5.37s | 3.67s | 1.46x | 1.70s |
| 4.65s | 8.75s | 6.40s | 1.37x | 2.35s |

**The ratio shrinks as the import grows, and the absolute saving is flat.** The prediction was
backwards, and the reason is the thing to internalize:

> **Redundant imports parallelize across idle cores.** Eight workers each importing on eight free
> cores costs ~1x wall time, not 8x. xdist's N-way collection is wasted *CPU*, not wasted *time*.
> You only get paid for removing it if the cores were already busy.

So xdist's real, addressable overhead is just gateway boot: `subprocess.Popen` of a virgin
interpreter per worker, plus the execnet handshake. Constant, and small.

## Three traps that produced convincing wrong numbers

**1. The runner silently never ran.** `pytest_runtestloop` is a `firstresult` hook: the first impl
to return non-None wins. Registering with `trylast=True` handed the loop to pytest's builtin, which
returned `True`. Every test still passed, serially, and the "fork" benchmarks were plain serial
pytest for hours. The tell was CPU utilization: **0.9 cores busy on a 16-worker run** (`user 5m44s`
/ `real 7m14s`), against xdist's 8.9 (`user 18m30s` / `real 2m10s`). *A parallel runner that isn't
running looks like a slow parallel runner, not like a broken one.* Check `user`/`real` before
believing any parallel benchmark.

**2. Benchmarking a suite that can't fork.** With the hook fixed, the reference suite refuses outright:

```
--parallel=fork requires a quiet process at fork time, but 1 non-main thread(s)
are running (Thread-1 (_monitor)).
```

`litestar.logging.standard.LoggingQueueListener` starts a logging thread **at import**. This is the
predicted #1 hazard arriving immediately: you cannot stop threads you don't own. Android's zygote
only gets away with fork because `ZygoteHooks.preFork()` calls `Daemons.stop()` and restarts them
after: it owns the entire runtime. A pytest plugin owns none of it.

**3. `time.sleep()` is not an import.** The first synthetic modelled import cost as a sleep. Sleep
parallelizes perfectly, so it showed fork's advantage *growing* with import weight, the opposite of
the truth. Model CPU-bound work with CPU-bound work.

## What was actually confirmed

- **xdist cannot fork, and it isn't close.** execnet's `get_execmodel()` closes over
  `thread`/`main_thread_only`/`eventlet`/`gevent` with an exhaustive `else: raise`; workers are
  spawned `subprocess.Popen([python, "-u", "-c", bootstrap])`. There is nothing to inherit. The
  ground is genuinely unoccupied.
- **The stdlib `random` frozen-seed hazard does not apply to CPython.** `Lib/random.py:992` does
  `_os.register_at_fork(after_in_child=_inst.seed)`: children reseed automatically. Verified by
  forking after `random.seed(42)`: children diverge. The Spring
  [#113](https://github.com/rails/spring/issues/113) precedent that froze a test seed for five years
  is *Ruby*, whose `Random` has no such hook. Libraries that seed their own state (numpy, faker)
  still need checking; stdlib does not.
- **The zygote effect is real but narrow.** Forking a parent that has already paid a 904ms
  `create_app()` costs **1.56ms/child, flat out to 248 children**. Whether that is worth anything
  depends entirely on whether the suite is CPU-bound; see below.
- **Dynamic scheduling beat static by only 6%** on the reference suite (411.5s → 386.5s), far under the
  1.43x predicted from per-module costs. Both numbers are void anyway (trap 1), but the
  claim-counter queue is the right design regardless: a static deal must guess a group's cost from
  its test *count*, and on a suite whose modules ranged 0.1s–167s that guess is bad.

## Where this leaves the project

Fork mode is worth shipping for the **inner loop**, the same niche Discord's pytest daemon targets
(median 20s → 2s) without inheriting their hard problem (correct incremental reload of a long-lived
dirty process; a forked child is a throwaway copy and the parent is never mutated).

It is **not** a CI play, and the prior art says so too: DHH removed Spring from Rails defaults in
[rails/rails#42997](https://github.com/rails/rails/pull/42997) with the reason *"Faster computers
have meant that most apps won't see a big benefit... the pain of dealing with the occasional spring
issue is no longer warranted by default."* The cost of a preloader is constant; the benefit shrinks
with hardware. That is the same shape as the flat 1.5–2.3s measured above.

Open questions, in order:
1. **Can a suite be made forkable?** The reference suite can't fork because of one third-party logging thread. A
   `pytest_parallex_pre_fork` / `post_fork` hook pair would let the suite author quiesce and restart
   what the plugin can't know about (Android's `Daemons.stop()`, Passenger's
   `:starting_worker_process`, Spring's `after_fork`; every prior system converged on this).
2. **Is the inner-loop win defensible against `--lf`/`-k`?** A dev rerunning one module already
   pays only that module's import. Measure fork against the workflows people actually use, not
   against a full-suite run.
3. **Does the zygote matter for anyone?** It only pays where per-test setup is expensive AND
   CPU-bound. the reference suite's setup is expensive but I/O-bound (Postgres), so it wouldn't have helped there.

---

# Does hoisting session fixtures buy wall time? Mostly no (2026-07-15)

Written after the session-fixture hoisting feature landed, because the obvious pitch for it
("xdist boots your container 8 times, we boot it once, so we're 8x faster on setup") is
wrong for the same reason trap 3 above is wrong. It's worth writing down twice.

## Bottom line

**Hoisting saves CPU and resources, not wall time.** The reason to use it is that one
Postgres exists instead of eight (memory, disk, Docker daemon pressure, API quota, licence
seats), and that the fixture can be written the obvious way. Not the clock.

Measured, 16 cores, 8 workers:

| suite | xdist | fork | note |
|---|---|---|---|
| 4 modules, 64 quick tests | 1.64s | 1.13s | fork's flat startup saving |
| 4 modules, 32 slow tests (`-n 16`) | 2.75s | **4.32s** | **fork 1.6x SLOWER** |
| 8 modules, 2s *sleeping* session fixture | 3.16s | 2.49s | ~unchanged |
| 8 modules, 2s *CPU-bound* session fixture | 2.16s | 1.35s | ~unchanged |

## Why the fixture rows are so flat

xdist boots the fixture in all 8 workers **concurrently**, on cores that were idle anyway.
Eight 2-second boots cost ~2 seconds of wall clock, not 16. This is trap 3 again, one level
up: *redundant work parallelises across idle cores*. Hoisting removes 14 core-seconds of
waste and about 0.7 wall-seconds, and you only get paid for the difference if those cores
had something better to do, that is, if workers outnumber cores, or the resource itself
serialises (one Docker daemon, one DB accepting CREATEs, a rate-limited API).

So the honest claim is the resource one. Eight containers is 8x the RAM whether or not it
costs 8x the seconds.

## The row that loses

`group_by_scope` hands each worker a whole **module**; xdist's default hands out individual
**tests**. So fork forks `min(num_workers, len(modules))` children and a suite with 4
modules uses 4 workers on a 16-core box, whatever `--parallel-workers` says. With enough
per-test work to swamp the ~1.5-2.3s startup saving, xdist wins outright: 2.75s vs 4.32s
above.

This is a real ceiling, not a tuning bug: module granularity is what lets a module-scoped
fixture stay set up for its whole module in one worker. Splitting tests across workers the
way xdist does would give that up. It's the correct trade for the feature, and it costs
exactly this.

---

# The module-granularity ceiling is gone (2026-07-16)

The "fork loses when there are fewer modules than cores" result above (fork 4.32s vs xdist
2.75s; fork 8.79s vs xdist 4.37s on one big module) was a **scheduler** limit, not a
fork limit, and it's fixed.

`plan_fork_claims` replaced `group_by_scope` as the fork work-list. A module whose tests
keep no coarse (package/module/class) fixture alive is dealt as individual tests any worker
can claim; a module that owns such a fixture still stays whole, so the fixture is set up
once for it. So pure-function-fixture modules (most modules) now fill every core, and
fork's warm start (no per-worker interpreter boot) makes it *faster* than xdist where it
used to be slower.

Re-measured, 16 cores, same shapes as above:

| suite | xdist | fork (before) | fork (after) |
|---|---|---|---|
| 4 modules, 64 quick tests | 1.39s | 1.13s | **0.33s** |
| 4 modules, 32 slow tests | 2.62s | 4.32s | **1.30s** |
| 1 module, 16 slow tests | 2.02s | 8.79s | **0.76s** |

The remaining honest ceiling: one big module with a real module fixture still runs on one
worker (splitting it would re-run the fixture per worker, which is the xdist behaviour this
plugin exists to avoid). If that single module dominates a suite's runtime, xdist can
spread it wider by paying for the fixture N times. Everything else fills the machine.
